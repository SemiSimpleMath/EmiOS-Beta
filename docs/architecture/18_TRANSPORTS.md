# Transports

## What it is

Transports are the surface adapters that bridge the outside world and the EmiOS room/manager loop. A user can reach the assistant via the local web UI (Socket.IO), via Twilio SMS, via a Slack channel the bot is in, or via a Telegram chat. Each surface has its own inbound mechanism (WebSocket event, signed HTTP POST), its own outbound delivery path (Socket.IO emit, REST/Twilio SDK call), and its own room-mapping convention. SMS/Slack/Telegram webhooks reach the local Flask app via whatever public tunnel the developer is running externally (ngrok, cloudflared, tailscale funnel, etc.) — there is no in-app tunnel helper. All four flows funnel into a single chokepoint — `RoomSessionManager` — which builds a transport-agnostic `InboundEnvelope`, runs the room manager, and returns an `OutboundIntent` that the surface-specific transport then delivers.

Cross-links: see `docs/architecture/03_ROOMS.md` for the room contract and `docs/architecture/02_MANAGERS.md` for the manager loop these envelopes feed.

## The four transports

| Surface  | Inbound mechanism                          | Outbound mechanism                              | Typical room                       | Who initiates    |
|----------|--------------------------------------------|-------------------------------------------------|------------------------------------|------------------|
| UI       | Socket.IO event `register_chat_client` + assistant pushes via `socket_emit` event | `socketio.emit("user_message_data", ...)` to bound socket | `master_room` (and other UI rooms: `music`, `progress`) | User             |
| SMS      | Twilio webhook POST `/twilio/sms` (form-encoded) | `TwilioSmsService.send_sms()` (REST via `twilio` SDK)     | Per-number, e.g. `taylor`, `jamie` (mapped from `From`) | User (sender)    |
| Slack    | Slack Events API POST `/slack/events` (JSON) | `SlackTool.handle_send_message()` (Slack Web API) | `slack/<channel_id>` (auto-derived) | Channel member   |
| Telegram | Telegram webhook POST `/telegram/webhook` (JSON) | `TelegramBotService.send_message()` (Bot API)   | `telegram/<chat_id>` (auto-derived) | Chat participant |

All four surfaces also support assistant-initiated outbound: any pipeline that produces a `UserMessage` with `event_topic = "socket_emit"` is dispatched by `EmiEventRelay._emit_message` to the right transport based on the message's `metadata.reply_to.type` (`socketio` / `twilio_sms` / `telegram` / `slack`). Synchronous request/reply is also delivered without going through the relay — see "Reply routing" below.

## The transport-agnostic envelope

Every inbound, regardless of surface, is normalized into an `InboundEnvelope` (`app/assistant/room_session_manager/contracts.py:7`):

```python
@dataclass
class InboundEnvelope:
    surface: str                 # "ui" | "sms" | "slack" | "telegram"
    room_id: str                 # logical EmiOS room (e.g. "master_room", "slack/C08AB0R54HM")
    context_id: str              # sub-channel within the room ("main" by default)
    request_id: str              # uuid; key for reply_router and persistence
    speaker_name: str            # display name as it should appear in chat
    speaker_id: str              # internal id (e.g. "sms:+1415...", "ui:user")
    speaker_external_id: Optional[str]  # raw transport id (Slack U..., Telegram numeric, phone number)
    content: str                 # plain-text body (with [emi_image: …] markers for attachments)
    timestamp_local: str
    inbound_line: str            # pre-rendered "[ts] Sender: text" line
    transport_message_id: str    # Twilio MessageSid / Slack message_ts / Telegram message_id
    transport_from: str
    transport_to: str
    reply_to: Dict[str, Any]     # {"type": "twilio_sms", "to": ..., "from": ...} etc.
    metadata: Dict[str, Any]     # room_mode, session ids, attachments, ...
    extras: Dict[str, Any]       # surface-private bag (socket_id, channel_id, ...)
```

Each surface's inbound service (`app/assistant/room_session_manager/services/surfaces/`) is responsible for filling this in:

- `ui_inbound_service.py` builds `reply_to = {"type": "socketio", "room_id": room_id}`.
- `sms_inbound_service.py:69` builds `reply_to = {"type": "twilio_sms", "to": from_number, "from": to_number, ...}`.
- `slack_inbound_service.py` builds `reply_to` keyed to channel + thread.
- `telegram_inbound_service.py` builds `reply_to = {"type": "telegram", "chat_id": ...}`.

The reverse contract is `OutboundIntent` (`contracts.py:30`):

```python
@dataclass
class OutboundIntent:
    request_id: str
    room_id: str
    room_surface: str
    room_context_id: str
    reply_text: str
    send: bool                  # honor send_reply gate from the inbound call
    delivery_mode: str
    reply_to: Dict[str, Any]
    payload: Dict[str, Any]
    metadata: Dict[str, Any]
```

`RoomSessionManager._deliver_outbound` calls the surface adapter's `send_outbound(reply_text)` callable, which in turn calls one of `UiRoomTransport.send_reply` / `SmsRoomTransport.send_reply` / `SlackRoomTransport.send_reply` / `TelegramRoomTransport.send_reply`. The four `*_transport.py` files in `app/assistant/room_session_manager/services/room_transports/` are intentionally tiny — each is just an adapter onto the underlying service (`TwilioSmsService`, `TelegramBotService`, `SlackTool`, or the `EventHub` for UI).

## WebSocket / UI

### Handshake and binding

The page loads, opens a Socket.IO connection, and emits `register_chat_client` with `{room_id: "master_room", since_ts?: <iso>}`. The server-side handler (`app/socket_handlers.py:118`):

1. Pulls `socket_id = request.sid`.
2. Calls `DI.socket_manager.bind(room_id, socket_id)` — this writes the `room_id → socket_id` mapping and returns any prior socket that was displaced.
3. If a prior socket was displaced (e.g. another tab opened), emits `socket_hijacked` to the displaced socket and force-disconnects it. The displaced tab's UI shows a "this tab was taken over" indicator.
4. If `since_ts` was provided (hot reconnect), calls `_replay_missed_to_socket()` which queries `blackboard.get_recent_chat_since_utc()` and replays up to `_REPLAY_CAP = 50` assistant messages. First-load (no `since_ts`) does NOT replay — the page-load path uses `/api/chat/history` for a clean restore.

A heartbeat handler (`socket_handlers.py:180`) accepts client-initiated `heartbeat` events, refreshes `last_seen_utc` on the binding, and acks back with the server clock for skew debugging.

### SocketManager — the room↔socket map

`app/services/socket_manager.py:15` is the single owner of the live `room_id ↔ socket_id` mapping. Three dicts under one `RLock`: `_rooms`, `_sockets`, `_last_seen`.

> Invariant (current): every `room_id` maps to **at most one** `socket_id`, and vice versa. `bind()` overwrites and returns the displaced socket id so the caller can notify and force-disconnect.

Three notable surface area points:

- `bind(room_id, socket_id)` (line 51) does the mutation and runs **bind-churn detection** — if a single `room_id` rebinds 5+ times in 60s it logs a WARNING ("client may be flapping"). This catches Cloudflare-tunnel flap loops without needing log grep.
- `emit_to_room(room_id, emit_fn)` (line 162) holds the lock across the lookup AND the emit call. Use this instead of `resolve_socket()`-then-emit, because the sweeper (below) can evict between the two calls and the emit silently drops.
- `RoomNotBound` is raised on every miss — there is no fallback. Callers must decide whether to log+skip or propagate.

### The stale-socket sweeper

`SocketManager.start_stale_sweeper(interval_s=60.0, max_age_s=90.0)` is started from `app/create_app.py:92` right after `initialize_system()`. A daemon thread (`socket-manager-sweeper`) calls `sweep_stale(90.0)` every 60s, evicting any socket whose `last_seen_utc` is older than 90s. This catches "zombie" sockets where TCP died silently (Cloudflare tunnel drops, laptop sleep) and Socket.IO never fired its `disconnect` event. Paired with the client-side heartbeat, the worst case is ~90s of drift before the binding is freed for the next client.

## SMS via Twilio

### Inbound

Route: `POST /twilio/sms` in `app/routes/twilio_sms.py:176`.

Twilio POSTs `application/x-www-form-urlencoded` with `From`, `To`, `Body`, `MessageSid`. The flow:

1. **Signature verification** (`_verify_twilio_signature`, line 66) — `RequestValidator(TWILIO_AUTH_TOKEN)` checks `X-Twilio-Signature` against the full public URL + form params. Behind a Cloudflare tunnel, `X-Forwarded-Proto` and `X-Forwarded-Host` are preferred over `request.url`. The function fail-loudly raises if `TWILIO_AUTH_TOKEN` is missing — there is no silent skip. Bypass for `127.0.0.1`/`localhost` is gated on both `_dev_tools_enabled()` AND the explicit `EMI_TWILIO_SKIP_SIG_LOCALHOST` env.
2. **Idempotency** — `is_duplicate_twilio(message_sid)` from `app/routes/webhook_dedup.py` (~10 min in-memory FIFO). Twilio retries on slow / non-2xx responses; without dedup the same SMS would be processed twice.
3. **Authorization** — number-based, fail-closed:
   - `TWILIO_AUTHORIZED_NUMBERS` env var (comma-separated E.164) — must contain `From`.
   - `TWILIO_NUMBER_ROOM_MAP` env var (`+14155551234=taylor,...`) — `From` must map to a configured room.
   Both are required (defense in depth). An empty allowlist means no inbound is accepted.
4. **Dispatch** — `current_app.DI.room_session_manager.handle_sms_inbound(...)` with `send_reply=True`.
5. Returns empty TwiML `<Response></Response>` immediately. Reply delivery is the manager loop's job, not Twilio's response body.

A dev-only simulator at `POST /twilio/sms/simulate` (line 260) accepts JSON or form payloads and lets you replay an SMS-shaped envelope without Twilio. Two modes: `room_manager` (synchronous run + return reply preview) and `event_hub` (publish via `_publish_twilio_inbound` for end-to-end flow testing).

### Outbound

`SmsRoomTransport.send_reply` calls `TwilioSmsService.send_sms` (`app/services/twilio_sms.py:51`) via the `twilio` Python SDK. Retries `429` / `5xx` with `[0.5, 1.5, 3.5]` second backoff, three attempts total. Returns the Twilio `sid` for persistence.

Assistant-initiated SMS (e.g. dayflow alerts) goes through `EmiEventRelay._emit_via_twilio_sms` based on `metadata.reply_to = {"type": "twilio_sms", "to": ..., "from": ...}`.

## Slack via Events API

### Inbound

Route: `POST /slack/events` in `app/routes/slack_events.py:222`.

Required env: `SLACK_SIGNING_SECRET`, `SLACK_AUTHORIZED_TEAM_IDS`, `SLACK_AUTHORIZED_CHANNELS`. Both authorization sets are fail-closed (empty = reject everything).

The handler runs eight checks before doing real work:

1. **Signature** (`_verify_slack_signature`, line 84) — HMAC-SHA256 over `"v0:" + ts + ":" + raw_body` keyed by `SLACK_SIGNING_SECRET`. **Must use `request.get_data(as_text=False)`** — `request.form` would double-decode URL-encoded characters and break HMAC. Stale timestamps (skew > 5 min) rejected to prevent replay.
2. **URL verification handshake** — when you configure the Request URL in the Slack app admin, Slack POSTs `{type: "url_verification", challenge: "..."}` once. The handler echoes `challenge` back. (line 240)
3. Type check: only `event_callback` envelopes proceed.
4. **Idempotency** — `is_duplicate_slack(event_id)` to avoid double-processing on Slack's retry-on-3s-timeout behavior.
5. **Team allowlist** check.
6. **Event filter** (`_extract_message_event`, line 115) — drops `bot_id`/`bot_profile` events (the assistant's own messages — without this filter the assistant responds to itself in an infinite loop), `subtype in {bot_message, message_changed, message_deleted, channel_join, channel_leave, message_replied}`, and any message with empty text.
7. **Channel allowlist** check.
8. **Async dispatch** via `start_monitored_thread(owner="slack_events", ...)` running `_process_slack_inbound_async` which calls `room_session_manager.handle_slack_inbound`. The webhook itself returns `{"ok": true}` immediately to satisfy Slack's 3-second response deadline.

Display names are resolved via `SlackTool.resolve_speaker_name` against a process-local `_user_name_cache` (avoids hitting `users.info` on every message). Falls back to the `U03E7L283S9`-style id if lookup fails.

### Channel ↔ room mapping

Room ids are auto-derived: `slack/<channel_id>` (e.g. `slack/C08AB0R54HM`) via `make_slack_room_id` (`app/assistant/rooms/room_bootstrap.py:43`). On first contact, `ensure_slack_room` clones template files from `app/assistant/rooms/_templates/slack_standard/` into `app/assistant/rooms/slack/<channel_id>/` (identity, conversation, safety, policy, permissions, access). Templates substitute `{{ROOM_ID}}`, `{{CHANNEL_ID}}`, `{{DISPLAY_NAME}}`, `{{PRIMARY_USER_NAME}}`.

`configs/slack_room.json` is the simulator/poller-side config (not used by the events webhook — the webhook is config-driven through env vars). Keys:

```json
{
  "use_room_mode": true,
  "send_reply": true,
  "allow_real_slack_send": true,    // safety gate — SlackTool dry-runs unless true
  "message_persistence_mode": "global_blackboard_and_unified_log",
  "channel_id": "C08AB0R54HM",      // default channel for /slack/room/simulate use_latest=true
  "default_room_id": "justin",
  "poll_limit": 100,
  "poll_overlap_seconds": 120,
  "status_resource_file": "resources/status/resource_slack_room_status.json",
  "room_name_map": {"justin": "justin"}
}
```

### Outbound

Slack has **two distinct outbound codepaths** — don't conflate them. Both end in `slack_sdk.WebClient.chat_postMessage` underneath, but they are separate code:

1. **Synchronous inbound-reply** — `SlackRoomTransport.send_reply(*, channel_id, body, thread_ts="")` (`room_transports/slack_transport.py`) calls `SlackTool().handle_send_message({channel_id, text, thread_ts?})`. This is the in-thread reply on the inbound request. `SlackTool` enforces the `allow_real_slack_send` gate from `configs/slack_room.json` — when false, it returns a dry-run string and does NOT hit the Slack API. This is the "I forgot to flip the safety switch" guard for shipping.
2. **Asynchronous relay** — when a background event publishes a `socket_emit` whose `reply_to.type == "slack"`, `EmiEventRelay._emit_via_slack` calls `app/services/slack.py::SlackService.send_message(channel_id=, text=, thread_ts=)`. `SlackService` is a minimal sender keyed off `SLACK_TOKEN` / `EMI_SLACK_TOKEN` / `SLACK_BOT_TOKEN` (first non-empty); it does **not** consult the `allow_real_slack_send` gate, so agent-initiated Slack sends go out for real.

Bot-vs-human distinction is enforced on inbound only (filter in `_extract_message_event`). On outbound, the assistant always posts as the bot user the OAuth token belongs to.

> Note: there is a separate dev simulator `POST /slack/room/simulate` in `slack_room.py` that supports a `use_latest=true` mode — pulls the latest non-the assistant message from a channel via `SlackTool.handle_get_messages` and runs it through the manager loop without needing Slack to actually POST.

## Telegram webhook

### Inbound

Route: `POST /telegram/webhook` in `app/routes/telegram_webhook.py:120`.

Required env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `TELEGRAM_AUTHORIZED_CHAT_IDS`.

1. **Secret verification** — `X-Telegram-Bot-Api-Secret-Token` header must equal `TELEGRAM_WEBHOOK_SECRET`. This is the secret you configured when calling `setWebhook` against the Telegram Bot API (out of band — see below).
2. **Idempotency** — `is_duplicate_telegram(update_id)`.
3. **Message extraction** (`_extract_message_payload`) — pulls `chat.id`, `text`, `from.username`/`first_name`/`last_name`. Uses `resolve_display_name(prefer_external_id_for_participant=True)` so unidentified senders show as their numeric id rather than colliding on first names.
4. **Chat allowlist** — `chat_id` must be in `TELEGRAM_AUTHORIZED_CHAT_IDS`. Webhook secret proves Telegram's servers POSTed it, but the bot is still publicly addressable, so chat-level authz is mandatory on top.
5. **Async dispatch** — same `start_monitored_thread` pattern as Slack. The webhook returns `{"ok": true}` immediately.

Room ids: `telegram/<chat_id>` (`make_telegram_room_id`, `room_bootstrap.py:30`). First contact triggers `ensure_telegram_room` — clones template files from `app/assistant/rooms/_templates/telegram_standard/` into `app/assistant/rooms/telegram/<chat_id>/`.

### Outbound

`TelegramRoomTransport.send_reply` calls `TelegramBotService.send_message` (`app/services/telegram_bot.py:49`). Direct HTTPS POST to `https://api.telegram.org/bot<TOKEN>/sendMessage` — no SDK dependency. Honors Telegram's `Retry-After` header on `429` (capped at 30s); retries up to `TELEGRAM_SEND_MAX_ATTEMPTS` (default 3, max 4). Returns the `message_id` for persistence.

Assistant-initiated Telegram messages go through `EmiEventRelay._emit_via_telegram` based on `metadata.reply_to = {"type": "telegram", "chat_id": ...}`.

### Setup (out-of-band)

Telegram does not have a "configure webhook in admin UI" flow. You must `POST` to the Telegram Bot API once:

```
POST https://api.telegram.org/bot<TOKEN>/setWebhook
{
  "url": "https://<your-public-host>/telegram/webhook",
  "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"
}
```

The `secret_token` here is what Telegram echoes back in the `X-Telegram-Bot-Api-Secret-Token` header on every inbound. There is no in-app helper for this — set it manually after pointing your tunnel at the local Flask app.

## Public tunneling

EmiOS does not bundle a tunnel. To expose the local Flask app to Twilio / Slack / Telegram webhooks during development, run whatever tunnel you prefer (ngrok, cloudflared, tailscale funnel, …) externally and paste the resulting URL into each surface's webhook config. Tunnel URLs from free-tier services are ephemeral; expect to redo all three on every restart unless your plan reserves a domain.

## RoomSessionManager — the central hub

`app/assistant/room_session_manager/room_session_manager.py` (~1171 lines) holds two classes: `InboundSurfaceAdapter` (the per-surface lambda bundle — `append_inbound` / `append_outbound` / `send_outbound`) and `RoomSessionManager`, the central hub every transport flows through. Public entry points on `RoomSessionManager`:

- `handle_ui_inbound(socket_id, body, room_id, ...)`
- `handle_sms_inbound(from_number, to_number, body, message_sid, room_id, ...)`
- `handle_slack_inbound(channel_id, body, room_id, message_ts, sender_name, sender_id, thread_ts, image_paths, ...)`
- `handle_telegram_inbound(chat_id, body, room_id, message_id, sender_name, sender_id, ...)`

Each delegates to `surface_handler_factory.get(<surface>).handle(self, ...)`. The four surface inbound services all do the same shape of work:

1. Normalize `message_persistence_mode`.
2. Resolve `request_id` (uuid if not provided).
3. Apply room-mode context (slash command extraction; see "Special inbound modes" below).
4. Try to short-circuit on a pending `ask_user` question via `question_answer_service.try_resolve`.
5. Build the `InboundEnvelope`.
6. Build a per-surface `InboundSurfaceAdapter` (lambda bundle for `append_inbound`, `append_outbound`, `send_outbound`).
7. Call the shared `_handle_inbound_generic` which: registers the reply route, persists inbound, prepares turn context (history + scope + resources), runs the manager, delivers outbound, persists outbound, fires room-summary if normal mode.

### Persistence policy modes

`message_persistence_mode` is one of two strings, validated in `room_policy_service.normalize_message_persistence_mode`:

| Mode | Effect |
|------|--------|
| `global_blackboard_only`             | Turn messages live in in-memory blackboard history only. Lost on restart. |
| `global_blackboard_and_unified_log`  | Turn messages also `save_to_unified_db` (the SQLite `unified_log_2026` table). |

The room's `policy.json` can further block unified-log persistence even when the call asks for it (`room_policy_allows_unified_log` checks `policy.write_unified_log` / `retention.write_unified_log` / `persistence.write_unified_log` / `ingestion.write_unified_log`). Result returned to the caller as `(persist_unified_log, persist_reason)` where reason is `mode_blackboard_only` / `room_policy_blocked` / `allowed`.

### Cross-room sharing rules

`access.json` per room can declare `shared_chat_room_ids: ["master_room", ...]`. `RoomHistoryBuilder.build_messages` honors this when seeding the room manager's history — messages from listed rooms are pulled in alongside the room's own messages, and tagged with `metadata.cross_room_tf = true` and `metadata.origin_room_id = <other_room>` so the agent prompt formatter can render them differently. `room_resource_loader.py:181` reads the access file and exposes the list as `room_shared_chat_room_ids` on the room context dict.

> Note: master_room's `access.json` does NOT itself declare `shared_chat_room_ids`. The cross-pollination is one-way — child rooms can pull master_room context, but master_room reads only its own history. (master_room access uses `"allowed_global_resources": ["all"]` and `"rag_scopes": ["chat", "memory"]` instead.)

## Special inbound modes

`RoomIngressService.apply_room_mode_context` runs early in every inbound flow and detects two things:

### Slash commands

`RoomSlashCommandRouter.route` (`services/room_slash_command_router.py`) recognizes:

| Command | Effect |
|---------|--------|
| `/plan [text]`      | Activate `planning_mode`; binds plan_session, sets `meta.room_mode = "planning_mode"` |
| `/done`, `/end`     | Close active plan/task/doc session (also clears any sticky `/actas` binding), short-circuit reply "X mode closed" |
| `/cancel`           | Close active plan session |
| `/task`, `/task:create` `[run\|load\|create] [name] [prompt]` | Run / load / create a task (mostly redirects to the `/task/create` page) |
| `/task:cancel`, `/task:exit`, `/task:done`, `/task:end` | Close task creation session |
| `/doc`, `/doc:create` `[create\|load] [md\|gdoc] [name] [prompt]` | Redirect to the `doc_editor` room (`/doc/editor`); the legacy in-room `doc_creation_mode` is being phased out |
| `/doc:cancel`, `/doc:exit`, `/doc:done`, `/doc:end` | Close doc creation session |
| `/play geoguessr [monitor]` | Activate `game_mode`; start screenshot timer |
| `/play stop\|pause\|resume\|next` | Game lifecycle controls |
| `/actas [self\|user]` | Set/clear a **sticky** principal for the room (`/actas self` acts as the assistant's principal; `/actas user`/`jukka`/`normal`/`off` exits; bare `/actas` shows current). Persists across messages until cleared or `/end` |
| `/pod expand <prefix>` | Deterministic, scope+authority-gated pod read — delegates to the `pod_command` service (`services/pod_command.py`) |

A `SlashCommandResult` either: (a) sets `room_mode` + `*_session_id` in metadata and lets the pipeline continue, or (b) sets `continue_pipeline=False` with an `early_result` that short-circuits the manager loop and replies immediately.

### Session mode routing

When `metadata.room_mode` is set to one of `planning_mode` / `task_creation_mode` / `doc_creation_mode` / `game_mode`, `_prepare_turn_context` consults `room_policy.mode_manager_overrides` to swap the manager (e.g. a doc-creation-specific manager). It also session-scopes the seeded history (`_build_room_session_seeded_messages` filters to messages tagged with the matching `plan_session_id` / `task_creation_session_id` / `doc_creation_session_id` / `geo_session_id`), so the pipeline only sees turns from the current session, not bleed from the previous mode.

## Reply routing

Two parallel mechanisms move replies back to the originator:

1. **Synchronous (manager-loop)** — `RoomSessionManager._deliver_outbound` calls the per-surface `adapter.send_outbound(reply_text)` in-thread on the inbound request, before the handler returns. This is the normal path. The reply text and delivery result are persisted as the outbound turn.
2. **Asynchronous (relay)** — any agent/control-node that produces a `UserMessage` with `event_topic = "socket_emit"` is consumed by `EmiEventRelay.socket_emit_handler` (`app/assistant/emi_event_relay/emi_event_relay.py:46`). The relay's `_resolve_reply_to(message)` looks up `metadata.reply_to` on the message, falling back to `DI.reply_router.get_route(request_id)`. Then `_emit_message` dispatches based on `reply_to.type`:
   - `socketio` → `_emit_via_socketio` (resolves `room_id` → `socket_id` via SocketManager, emits `user_message_data`).
   - `twilio_sms` → `_emit_via_twilio_sms`.
   - `telegram` → `_emit_via_telegram`.
   - `slack` → `_emit_via_slack` (resolves `channel_id` and optional `thread_ts`, dispatches to `app/services/slack.py::SlackService.send_message`).

The `ReplyRouter` (`app/services/reply_router.py`) is a thread-safe `request_id → reply_to` map with a 24h TTL. It's the backbone for "an event happened later that should be replied to the original requester" — every transport's inbound path calls `DI.reply_router.set_route(request_id, reply_to)` so any later background agent can look up where to send the reply.

## Known issues / planned work

- **Single-device socket binding (per-room)**. `SocketManager` enforces `room_id ↔ socket_id` 1:1. Opening the chat on phone evicts the desktop binding (the displaced socket gets `socket_hijacked` and is force-disconnected). Multi-device support requires moving `_rooms` from `dict[str, str]` to `dict[str, set[str]]`, broadcasting in `emit_to_room`, and re-thinking the `displaced_socket_id` return semantics. Tracked in memory as `project_socket_manager_multi_device`.
- **Webhook dedup is in-memory only**. `WebhookDedupCache` (10-min TTL, 2048 entries) is per-process. Restart Flask between original delivery and Twilio/Slack/Telegram retry → duplicate slips through. Downstream `message_persistence` does its own idempotency check on `message_sid`/`event_id`/`update_id` as a second line of defense.
- **First-load fallback room_id**. `register_chat_client` falls back to `"chat"` if the client doesn't send `room_id`. Server emits target `master_room`, so a fallback binding would silently receive nothing. Logged as WARNING (`app/socket_handlers.py:138`) but the only fix is updating the client to always send `{room_id: "master_room"}`.

## Key files

| File | Purpose |
|------|---------|
| `app/socket_handlers.py` | Socket.IO event handlers — `register_chat_client`, `disconnect`, `heartbeat`, music/progress/DJ events |
| `app/services/socket_manager.py` | `SocketManager` — room↔socket map, bind churn detection, stale-socket sweeper |
| `app/routes/twilio_sms.py` | Twilio inbound webhook + simulator |
| `app/services/twilio_sms.py` | `TwilioSmsService` — outbound SMS via Twilio SDK with retry |
| `app/routes/slack_events.py` | Slack Events API inbound webhook (signed, async dispatch) |
| `app/routes/slack_room.py` | Dev-only Slack inbound simulator (supports `use_latest`) |
| `app/assistant/lib/core_tools/slack/slack.py` | `SlackTool` — Slack Web API client (synchronous inbound-reply path), dry-run safety gate |
| `app/services/slack.py` | `SlackService.send_message` — minimal async-relay Slack sender (used by `EmiEventRelay._emit_via_slack`) |
| `app/routes/telegram_webhook.py` | Telegram webhook inbound |
| `app/services/telegram_bot.py` | `TelegramBotService` — outbound `sendMessage` via raw HTTPS |
| `app/routes/webhook_dedup.py` | `WebhookDedupCache` — in-memory idempotency for Twilio/Slack/Telegram retries |
| `app/assistant/room_session_manager/room_session_manager.py` | Central hub — surface adapters, generic inbound handler |
| `app/assistant/room_session_manager/contracts.py` | `InboundEnvelope`, `OutboundIntent` |
| `app/assistant/room_session_manager/services/surfaces/` | Per-surface inbound services (build envelope) |
| `app/assistant/room_session_manager/services/room_transports/` | Per-surface outbound transports (deliver reply) |
| `app/assistant/room_session_manager/services/room_slash_command_router.py` | `/plan`, `/task`, `/doc`, `/play` dispatch |
| `app/assistant/room_session_manager/services/room_policy_service.py` | persistence-mode + manager-name + authority resolution |
| `app/assistant/rooms/room_bootstrap.py` | `make_*_room_id`, `ensure_*_room` (template clone) |
| `app/assistant/rooms/_templates/{slack,telegram}_standard/` | Template room files cloned on first contact |
| `app/assistant/emi_event_relay/emi_event_relay.py` | Async outbound relay — `socket_emit` event consumer, fan-out to socketio/twilio/telegram |
| `app/services/reply_router.py` | `request_id → reply_to` map (24h TTL) |
| `configs/slack_room.json` | Slack simulator/poller config + `allow_real_slack_send` safety gate |

## How to add a new transport (sketch)

The four existing transports follow a strict pattern. To add a fifth (say, Discord):

1. **Build the inbound surface service** at `app/assistant/room_session_manager/services/surfaces/discord_inbound_service.py` modeled after `slack_inbound_service.py`. It takes raw transport fields, builds an `InboundEnvelope` with `surface="discord"`, and calls `manager._handle_inbound_generic(...)`.
2. **Register it** in `RoomSurfaceHandlerFactory._handlers` (`services/surfaces/factory.py:14`).
3. **Build the outbound transport** at `app/assistant/room_session_manager/services/room_transports/discord_transport.py` with a `send_reply(*, channel_id, body) -> str` method delegating to a low-level service in `app/services/discord_bot.py`.
4. **Add `RoomSessionManager._build_discord_adapter`** (mirrors `_build_slack_adapter`) wiring `append_inbound` / `append_outbound` / `send_outbound` lambdas onto the persistence service and transport.
5. **Add a public `handle_discord_inbound(...)`** method on `RoomSessionManager` that dispatches to `surface_handler_factory.get("discord")`.
6. **Add a webhook route** at `app/routes/discord_webhook.py` mirroring `telegram_webhook.py`: signature verify → idempotency dedup → allowlist → `start_monitored_thread` → `room_session_manager.handle_discord_inbound`. Add `is_duplicate_discord` to `webhook_dedup.py`.
7. **Add room mapping helpers** `make_discord_room_id` / `ensure_discord_room` in `room_bootstrap.py` and a template directory at `app/assistant/rooms/_templates/discord_standard/`.
8. **Wire reply_to type "discord"** into `EmiEventRelay._emit_message` so async agent-initiated outbound works.
9. **Add `RoomMessagePersistenceService.append_discord_inbound` / `_outbound`** mirroring the Slack methods.

The cookbook is mechanical — every step has a working precedent. The hard part is the OAuth/auth scheme and the rate-limit semantics of the new platform, both of which are platform-specific and live entirely in the inbound webhook handler and the low-level service.
