# master_room in-room doc_creation_mode — archived 2026-05-25

**DO NOT DELETE.** Snapshot of the orphan `master_room::doc_create` and
`master_room::doc_writer` agents that supported a now-retired inline
doc-creation path inside `master_room_manager`.

## Why archived

`master_room_manager` historically had **two independent paths to doc
creation**:

1. **Inline mode** — switchboard/slash router could flip master_room into
   `doc_creation_mode`, wiring `master_room::doc_create → master_room::doc_writer
   → doc_create_final_router_node → final_answer_node` directly inside
   master_room. The two agents in this archive were that path.
2. **Delegated** — the dedicated `doc_editor` room (its own ROOM.md +
   `doc_editor_manager`) handles all doc-creation flows.

Two paths to the same outcome is the known consistency tax (see also the
parallel `master_room_task_creation_2026_05_06` archive). The inline mode
was retired 2026-05-25 after `/doc` was changed to redirect to the
dedicated `/doc/editor` page (matching the `/task` pattern).

## What changed

In `app/assistant/rooms/master_room/policy.json` + `ROOM.md` (Phase 1, earlier 2026-05-25):
- Removed the `doc_creation_mode` entry from `mode_manager_overrides` (it
  was pointing to `master_room_doc_creation_manager`, which never existed
  in main).

In `app/assistant/room_session_manager/services/room_slash_command_router.py` (Phase 2a):
- `_handle_doc()` and `_handle_doc_load()` now return a `widget_data`
  redirect to `/doc/editor?...` instead of activating an in-room session.

In `app/assistant/multi_agents/master_room_manager/config.yaml` (Phase 3):
- Removed `master_room::doc_create` and `master_room::doc_writer` from the
  `agents:` block.
- Removed `doc_create_final_router_node` from the `control_nodes:` block.
- Removed the `flow.doc_creation_mode` line in `flow_config`.
- Removed the standalone `doc_creation_mode:` flow_config block.
- Removed the 3 state_map entries: `doc_create → doc_writer →
  doc_create_final_router_node → final_answer_node`.

## What's archived

The two agent directories:
- `app/assistant/agents/master_room/doc_create/`
  - `config.yaml`, `agent_form.py`, `prompts/`
- `app/assistant/agents/master_room/doc_writer/`
  - `config.yaml`, `agent_form.py`, `prompts/`

## What stayed live

All shared infrastructure still serves the legitimate `doc_editor_manager`
flow:
- `doc_editor::chat` agent (`app/assistant/agents/doc_editor/chat/`)
- `doc_editor::editor` agent (`app/assistant/agents/doc_editor/editor/`)
- `doc_create_final_router_node` (control node class — the
  `doc_editor_manager` still imports it via its own `control_nodes:`
  entry; we only removed the instance from `master_room_manager`).
- `doc_writer_pre_node` (control node class — only used by
  `doc_editor_manager`).
- `DocCreationSessionService` (still stamps `room_mode=doc_creation_mode`
  on inbound envelopes destined for the dedicated `doc_editor` room).
- `room_ingress_service.py` session-restoration logic — scoped to
  `room_id == "doc_editor"`, so it never fires for master_room.

## Restoration

Move the two agent dirs back to `app/assistant/agents/master_room/`,
restore the deleted entries in `master_room_manager/config.yaml`, restore
the `doc_creation_mode` override in `master_room/policy.json` and
`ROOM.md`, and revert `_handle_doc()` and `_handle_doc_load()` in
`room_slash_command_router.py` to their pre-2026-05-25 session-activation
forms. Verify the dedicated `doc_editor` room still works (its config
wasn't touched).
