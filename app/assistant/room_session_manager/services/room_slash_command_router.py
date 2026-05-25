from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result type returned by every command handler.
#
#   normalized_body  – text to pass on to the agent (empty = early-return only)
#   meta             – updated metadata dict (room_mode, session_id, etc.)
#   early_result     – if set, short-circuit the pipeline and reply immediately
#                      (same shape the ingress already uses: reply_text,
#                       outbound_intent, widget_data, …)
#   continue_pipeline – True  → run the full agent pipeline
#                       False → early-return (early_result is the response)
# ---------------------------------------------------------------------------
@dataclass
class SlashCommandResult:
    normalized_body: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    early_result: Optional[Dict[str, Any]] = None
    continue_pipeline: bool = True


class RoomSlashCommandRouter:
    """
    Single-responsibility handler for room-level slash commands.

    Each public method is named after the command it handles.
    apply_room_mode_context in RoomIngressService calls route() and gets back
    a SlashCommandResult — no command logic lives in the ingress.
    """

    def __init__(
        self,
        *,
        plan_sessions,
        task_sessions,
        doc_sessions,
        geo_sessions,
        actas_sessions,
    ) -> None:
        self._plan = plan_sessions
        self._task = task_sessions
        self._doc = doc_sessions
        self._geo = geo_sessions
        self._actas = actas_sessions

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def route(
        self,
        *,
        cmd_name: str,
        cmd_payload: str,
        room_id: str,
        surface: str,
        context_id: str,
        sender_identity: str,
        meta: Dict[str, Any],
    ) -> Optional[SlashCommandResult]:
        """
        Dispatch to the right handler.
        Returns None if the command is not recognised (caller treats as normal msg).
        """
        # --- planning ---
        if cmd_name == "plan":
            return self._handle_plan(
                cmd_payload=cmd_payload, room_id=room_id, surface=surface,
                context_id=context_id, sender_identity=sender_identity, meta=meta,
            )
        if cmd_name in {"done", "end"}:
            return self._handle_done(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )
        if cmd_name == "cancel":
            return self._handle_cancel_plan(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )

        # --- task creation ---
        if cmd_name in {"task", "task:create"}:
            return self._handle_task(
                cmd_payload=cmd_payload, room_id=room_id, surface=surface,
                context_id=context_id, sender_identity=sender_identity, meta=meta,
            )
        if cmd_name in {"task:cancel", "task:exit", "task:done", "task:end"}:
            return self._handle_task_cancel(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )

        # --- doc creation ---
        if cmd_name in {"doc", "doc:create"}:
            return self._handle_doc(
                cmd_payload=cmd_payload, room_id=room_id, surface=surface,
                context_id=context_id, sender_identity=sender_identity, meta=meta,
            )
        if cmd_name in {"doc:cancel", "doc:exit", "doc:done", "doc:end"}:
            return self._handle_doc_cancel(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )

        # --- game (geoguessr) ---
        if cmd_name == "play":
            return self._handle_play(
                cmd_payload=cmd_payload, room_id=room_id, surface=surface,
                context_id=context_id, sender_identity=sender_identity, meta=meta,
            )

        # --- acting-as (principal toggle) ---
        if cmd_name == "actas":
            return self._handle_actas(
                cmd_payload=cmd_payload, room_id=room_id, surface=surface,
                context_id=context_id, meta=meta,
            )

        return None  # unknown — caller handles as normal message

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _handle_plan(self, *, cmd_payload, room_id, surface, context_id,
                     sender_identity, meta) -> SlashCommandResult:
        binding = self._plan.activate_room_binding(
            room_id=room_id, surface=surface, context_id=context_id,
            initiated_by=sender_identity,
        )
        meta = dict(meta)
        meta["room_mode"] = "planning_mode"
        meta["plan_session_id"] = str(binding.get("plan_session_id") or "").strip()
        meta["plan_command"] = "plan"
        return SlashCommandResult(
            normalized_body=cmd_payload.strip() or "Let's start planning.",
            meta=meta,
        )

    def _handle_done(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        # /end always also clears any sticky actas binding alongside other modes.
        # This keeps /end as a single "back to defaults" exit regardless of
        # which sticky state happens to be active.
        actas_cleared = False
        if self._actas is not None:
            actas_cleared = self._actas.clear_principal(
                room_id=room_id, surface=surface, context_id=context_id,
            )
        closed = self._plan.deactivate_room_binding(
            room_id=room_id, surface=surface, context_id=context_id, reason="done",
        )
        if closed:
            reply, closed_key = "Planning mode closed.", "plan_mode_closed"
        else:
            closed = self._task.deactivate_room_binding(
                room_id=room_id, surface=surface, context_id=context_id, reason="done",
            )
            if closed:
                reply, closed_key = "Task creation session ended.", "task_creation_mode_closed"
            elif self._doc is not None:
                closed = self._doc.deactivate_room_binding(
                    room_id=room_id, surface=surface, context_id=context_id, reason="done",
                )
                reply = "Document creation session ended." if closed else "No active session."
                closed_key = "doc_creation_mode_closed"
            else:
                reply, closed_key = "No active session.", "task_creation_mode_closed"
        if actas_cleared and reply == "No active session.":
            reply = "Acting-as cleared (back to user)."
        elif actas_cleared:
            reply = f"{reply} Acting-as cleared (back to user)."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                closed_key: bool(closed),
                "actas_cleared": bool(actas_cleared),
            },
        )

    # ------------------------------------------------------------------
    # Acting-as (principal toggle)
    # ------------------------------------------------------------------

    def _handle_actas(self, *, cmd_payload, room_id, surface, context_id, meta) -> SlashCommandResult:
        """Set/clear the sticky principal for this room.

        Forms:
          /actas              → show current principal
          /actas self         → enter self-mode (acting as the assistant's principal)
          /actas user         → exit (also accepts /actas jukka, /actas normal)

        Mode persists across messages until cleared via `/actas user` or `/end`.
        """
        meta = dict(meta)
        arg = (cmd_payload or "").strip().lower()
        if self._actas is None:
            reply = "Acting-as service is not available in this room."
            return SlashCommandResult(
                meta=meta,
                continue_pipeline=False,
                early_result={
                    "reply_text": reply,
                    "reply_payload": {"chat": reply},
                    "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                },
            )

        # /actas (no arg) → show current
        if not arg:
            current = self._actas.get_principal(
                room_id=room_id, surface=surface, context_id=context_id,
            )
            reply = f"Currently acting as: {current}."
            return SlashCommandResult(
                meta=meta,
                continue_pipeline=False,
                early_result={
                    "reply_text": reply,
                    "reply_payload": {"chat": reply},
                    "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                    "actas_principal": current,
                },
            )

        # /actas user|jukka|normal → clear
        if arg in {"user", "jukka", "normal", "off"}:
            cleared = self._actas.clear_principal(
                room_id=room_id, surface=surface, context_id=context_id,
            )
            reply = "Back to acting as user." if cleared else "Already acting as user."
            return SlashCommandResult(
                meta=meta,
                continue_pipeline=False,
                early_result={
                    "reply_text": reply,
                    "reply_payload": {"chat": reply},
                    "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                    "actas_principal": "user",
                    "actas_cleared": bool(cleared),
                },
            )

        # /actas self → set to the assistant's self principal (e.g. "emi")
        if arg == "self":
            from app.assistant.emi_accounts import _assistant_self_principal
            principal = _assistant_self_principal()
            self._actas.set_principal(
                room_id=room_id, surface=surface, context_id=context_id,
                principal=principal,
            )
            reply = (
                f"Now acting as {principal}. All subsequent tasks run under {principal}'s "
                f"identity until you `/actas user` or `/end`."
            )
            return SlashCommandResult(
                meta=meta,
                continue_pipeline=False,
                early_result={
                    "reply_text": reply,
                    "reply_payload": {"chat": reply},
                    "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                    "actas_principal": principal,
                },
            )

        # Unknown principal name. (Future: /actas katy, /actas peter, etc.)
        reply = (
            f"Unknown principal '{arg}'. Usage: `/actas self` to enter, "
            f"`/actas user` (or `/actas` for status) to exit."
        )
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
            },
        )

    def _handle_cancel_plan(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        closed = self._plan.deactivate_room_binding(
            room_id=room_id, surface=surface, context_id=context_id, reason="cancel",
        )
        reply = "Planning mode cancelled." if closed else "No active planning session."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                "plan_mode_closed": bool(closed),
            },
        )

    # ------------------------------------------------------------------
    # Task creation
    # ------------------------------------------------------------------

    def _handle_task(self, *, cmd_payload, room_id, surface, context_id,
                     sender_identity, meta) -> SlashCommandResult:
        meta = dict(meta)
        payload = cmd_payload.strip()

        # /task run <name> — run a compiled task directly
        if payload.lower().startswith("run"):
            task_name = payload[3:].strip()
            if not task_name:
                return SlashCommandResult(
                    meta=meta,
                    continue_pipeline=False,
                    early_result={"reply_text": "Usage: /task run <task name>"},
                )
            return SlashCommandResult(
                normalized_body=f"run task {task_name}",
                meta=meta,
            )

        # /task load <name> — redirect to task creation page
        if payload.lower().startswith("load"):
            task_name = payload[4:].strip()
            redirect_url = f"/task/create?load={task_name}" if task_name else "/task/create"
            return SlashCommandResult(
                meta=meta,
                continue_pipeline=False,
                early_result={
                    "reply_text": "",
                    "widget_data": [{"data_type": "redirect", "url": redirect_url}],
                },
            )

        # /task create [prompt] — redirect to task creation page
        create_prompt = payload[6:].strip() if payload.lower().startswith("create") else payload
        redirect_url = "/task/create"
        if create_prompt:
            import urllib.parse
            redirect_url += f"?prompt={urllib.parse.quote(create_prompt)}"
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": "",
                "widget_data": [{"data_type": "redirect", "url": redirect_url}],
            },
        )

    @staticmethod
    def _fetch_task_spec_content(*, task_name: str) -> tuple[str, dict | None]:
        """Load a task spec and return (markdown, spec_dict) for editing.

        Prefers task_spec.json (structured) → strips enrichment, renders
        prose-only markdown. Falls back to task_spec.md if no JSON exists.
        Returns (markdown, spec_dict) where spec_dict may be None for raw markdown files.
        """
        query = str(task_name or "").strip()
        if not query:
            return "", None
        try:
            from app.routes.task_graph_route import _find_task_spec

            spec_path, _task_id = _find_task_spec(query)
            if spec_path is None:
                return "", None

            # Try loading the structured JSON spec first.
            json_path = spec_path.parent / "task_spec.json"
            if json_path.exists():
                import json
                from app.assistant.lib.task_utils.task_spec_model import TaskSpec
                spec_dict = json.loads(json_path.read_text(encoding="utf-8"))
                spec = TaskSpec.model_validate(spec_dict)
                spec.reset_all_planning()
                return spec.to_markdown(), spec.model_dump()

            # Fall back to raw markdown.
            return spec_path.read_text(encoding="utf-8"), None
        except Exception as e:
            logger.error("task load: failed reading task spec for %r: %s", query, e)
            logger.debug("task load spec read exception", exc_info=True)
            raise

    def _handle_task_cancel(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        closed = self._task.deactivate_room_binding(
            room_id=room_id, surface=surface, context_id=context_id, reason="cancel",
        )
        reply = "Task creation cancelled." if closed else "No active task creation session."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                "task_creation_mode_closed": bool(closed),
            },
        )

    # ------------------------------------------------------------------
    # Doc creation
    # ------------------------------------------------------------------
    #
    # `/doc` (and aliases) now redirect to the dedicated `doc_editor` room
    # (template: doc_editor.html; URL: /doc/editor). The legacy in-room
    # doc_creation_mode flow inside master_room_manager is being phased out
    # (see project_master_room_mode_overrides_cleanup memo). Matches the
    # `/task` pattern, which redirects to `/task/create` page.
    #
    # Query params the doc_editor page understands:
    #   ?load=<name>    — load an existing markdown doc
    #   ?gdoc=<name>    — load an existing Google Doc
    #   ?prompt=<text>  — seed an initial prompt for the chat

    def _handle_doc(self, *, cmd_payload, room_id, surface, context_id,
                    sender_identity, meta) -> SlashCommandResult:
        import urllib.parse
        meta = dict(meta)
        payload = cmd_payload.strip()

        # /doc load [gdoc|md] <name> → redirect to editor with load/gdoc param
        if payload.lower().startswith("load"):
            return self._handle_doc_load(
                load_rest=payload[4:].strip(), room_id=room_id, surface=surface,
                context_id=context_id, sender_identity=sender_identity, meta=meta,
            )

        # /doc [create] [md|gdoc] [name] [prompt] — strip leading subcommand
        # keywords, treat the remainder as the seed prompt for the editor.
        tokens = payload.split(None, 2)
        if tokens and tokens[0].lower() == "create":
            tokens = tokens[1:]
        if tokens and tokens[0].lower() in {"md", "gdoc"}:
            tokens = tokens[1:]
        doc_prompt = ""
        if tokens:
            # Treat the rest as a free-form prompt (don't pre-split name vs prompt;
            # the editor surfaces a name field separately).
            doc_prompt = " ".join(tokens).strip()

        redirect_url = "/doc/editor"
        if doc_prompt:
            redirect_url += f"?prompt={urllib.parse.quote(doc_prompt)}"
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": "",
                "widget_data": [{"data_type": "redirect", "url": redirect_url}],
            },
        )

    def _handle_doc_load(self, *, load_rest, room_id, surface, context_id,
                         sender_identity, meta) -> SlashCommandResult:
        """`/doc load [md|gdoc] <name>` → redirect to /doc/editor with the
        right query param (?load=<name> for md, ?gdoc=<name> for gdoc).
        """
        import urllib.parse
        doc_type = "md"
        tokens = load_rest.split(None, 1)
        if tokens and tokens[0].lower() in {"md", "gdoc"}:
            doc_type = tokens[0].lower()
            load_rest = tokens[1].strip() if len(tokens) > 1 else ""
        doc_name = load_rest.strip()

        if not doc_name:
            return SlashCommandResult(
                meta=meta,
                continue_pipeline=False,
                early_result={"reply_text": "Usage: /doc load [md|gdoc] <name>"},
            )

        param = "gdoc" if doc_type == "gdoc" else "load"
        redirect_url = f"/doc/editor?{param}={urllib.parse.quote(doc_name)}"
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": "",
                "widget_data": [{"data_type": "redirect", "url": redirect_url}],
            },
        )

    def _handle_doc_cancel(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        closed = False
        if self._doc is not None:
            closed = self._doc.deactivate_room_binding(
                room_id=room_id, surface=surface, context_id=context_id, reason="cancel",
            )
        reply = "Document creation cancelled." if closed else "No active document creation session."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                "doc_creation_mode_closed": bool(closed),
            },
        )

    # ------------------------------------------------------------------
    # Game (GeoGuessr)
    # ------------------------------------------------------------------

    def _handle_play(self, *, cmd_payload, room_id, surface, context_id,
                     sender_identity, meta) -> SlashCommandResult:
        meta = dict(meta)
        tokens = (cmd_payload or "").strip().split()
        sub = tokens[0].lower() if tokens else ""

        if sub == "geoguessr":
            return self._handle_play_start(
                tokens=tokens, room_id=room_id, surface=surface,
                context_id=context_id, sender_identity=sender_identity, meta=meta,
            )
        if sub in {"stop", "end", "quit", "exit"}:
            return self._handle_play_stop(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )
        if sub == "pause":
            return self._handle_play_pause(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )
        if sub == "resume":
            return self._handle_play_resume(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )
        if sub == "next":
            return self._handle_play_next(
                room_id=room_id, surface=surface, context_id=context_id, meta=meta,
            )
        return None  # unknown /play subcommand

    def _handle_play_start(self, *, tokens, room_id, surface, context_id,
                           sender_identity, meta) -> SlashCommandResult:
        monitor_index = int(tokens[1]) if len(tokens) > 1 and tokens[1].isdigit() else 1
        if self._geo is not None:
            binding = self._geo.activate_room_binding(
                room_id=room_id, surface=surface, context_id=context_id,
                initiated_by=sender_identity,
            )
            session_id = str(binding.get("session_id") or "").strip()
            meta["room_mode"] = "game_mode"
            meta["geo_session_id"] = session_id
            meta["geo_monitor_index"] = monitor_index
            reply_to = meta.get("reply_to") or {}
            socket_id = str(reply_to.get("room") or "") if isinstance(reply_to, dict) else str(reply_to or "")
            tts_for_timer = bool((reply_to.get("tts_requested") if isinstance(reply_to, dict) else False))
            if tts_for_timer:
                self._geo.set_tts_requested(session_id=session_id, tts_requested=True)
            try:
                from app.assistant.game.geoguessr.geo_screenshot_timer import start_game_timer
                start_game_timer(
                    session_id=session_id, socket_id=socket_id,
                    room_id=room_id, surface=surface, context_id=context_id,
                    monitor_index=monitor_index, tts_requested=tts_for_timer,
                )
            except Exception:
                logger.debug("[RoomSlashCommandRouter] Failed starting geo timer", exc_info=True)
        monitor_note = f" (monitor {monitor_index})" if monitor_index != 1 else ""
        return SlashCommandResult(
            normalized_body=f"GeoGuessr is ready{monitor_note}. Type /play resume to start!",
            meta=meta,
        )

    def _handle_play_stop(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        closed = False
        if self._geo is not None:
            geo_binding = self._geo.get_active_room_binding(
                room_id=room_id, surface=surface, context_id=context_id,
            )
            if geo_binding:
                try:
                    from app.assistant.game.geoguessr.geo_screenshot_timer import stop_game_timer
                    stop_game_timer(session_id=str(geo_binding.get("session_id") or ""))
                except Exception:
                    logger.debug("[RoomSlashCommandRouter] Failed stopping geo timer", exc_info=True)
            closed = self._geo.deactivate_room_binding(
                room_id=room_id, surface=surface, context_id=context_id, reason="stop",
            )
        reply = "Game over! Thanks for playing." if closed else "No active game session."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
                "game_mode_closed": bool(closed),
            },
        )

    def _handle_play_pause(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        if self._geo is not None:
            geo_binding = self._geo.get_active_room_binding(
                room_id=room_id, surface=surface, context_id=context_id,
            )
            if geo_binding:
                try:
                    from app.assistant.game.geoguessr.geo_screenshot_timer import pause_game_timer
                    pause_game_timer(session_id=str(geo_binding.get("session_id") or ""))
                except Exception:
                    logger.debug("[RoomSlashCommandRouter] Failed pausing geo timer", exc_info=True)
        reply = "Game paused. Type /play resume to continue."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
            },
        )

    def _handle_play_resume(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        if self._geo is not None:
            geo_binding = self._geo.get_active_room_binding(
                room_id=room_id, surface=surface, context_id=context_id,
            )
            if geo_binding:
                try:
                    from app.assistant.game.geoguessr.geo_screenshot_timer import resume_game_timer
                    resume_game_timer(session_id=str(geo_binding.get("session_id") or ""))
                except Exception:
                    logger.debug("[RoomSlashCommandRouter] Failed resuming geo timer", exc_info=True)
        reply = "Game resumed!"
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
            },
        )

    def _handle_play_next(self, *, room_id, surface, context_id, meta) -> SlashCommandResult:
        meta = dict(meta)
        if self._geo is not None:
            geo_binding = self._geo.get_active_room_binding(
                room_id=room_id, surface=surface, context_id=context_id,
            )
            if geo_binding:
                sid = str(geo_binding.get("session_id") or "")
                try:
                    self._geo.reset_round(session_id=sid)
                except Exception:
                    logger.debug("[RoomSlashCommandRouter] Failed resetting geo session", exc_info=True)
                    raise
                try:
                    from app.assistant.game.geoguessr.geo_screenshot_timer import reset_round_timer
                    reset_round_timer(session_id=sid)
                except Exception:
                    logger.debug("[RoomSlashCommandRouter] Failed resetting round timer", exc_info=True)
                    raise
        reply = "New round started! I cleared my notes — let's see what the new location has in store."
        return SlashCommandResult(
            meta=meta,
            continue_pipeline=False,
            early_result={
                "reply_text": reply,
                "reply_payload": {"chat": reply},
                "outbound_intent": {"send": True, "reply_text": reply, "reply_payload": {"chat": reply}},
            },
        )
