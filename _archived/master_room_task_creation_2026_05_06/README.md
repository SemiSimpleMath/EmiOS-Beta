# master_room inline task_creation_mode — archived 2026-05-06

**DO NOT DELETE.** Snapshot of the orphaned `master_room::task_spec_writer` agent that supported a now-retired inline task-creation path inside `master_room_manager`.

## Why archived

`master_room_manager` historically had **two independent paths to task creation**:

1. **Inline mode** — switchboard could route into a `task_creation_mode` block that wired `task_spec::chat → task_spec_router_node → task_create_final_router_node → final_answer_node` directly inside master_room. The `master_room::task_spec_writer` agent was a part of this inline pipeline.
2. **Delegated** — handing off to the dedicated `task_spec_manager` room (its own room with full editor/edit-apply chain).

Two paths to the same outcome is a known consistency tax: maintenance burden, surprising UX (different flows depending on entry point), drift risk between the two paths. The inline mode was retired (2026-05-06).

## What changed

In `app/assistant/multi_agents/master_room_manager/config.yaml`:
- Removed `task_spec::chat` and `master_room::task_spec_writer` from the `agents:` block
- Removed `task_spec_router_node` and `task_create_final_router_node` from the `control_nodes:` block
- Removed the `mode_routing.task_creation_mode` entry
- Removed the standalone `task_creation_mode:` flow config block
- Removed the 3 state_map entries: `task_spec::chat → task_spec_router_node → task_create_final_router_node → final_answer_node`

In `app/assistant/control_nodes/task_create_final_router_node.py`:
- Dropped `master_room::task_spec_writer` from `_ACCEPTED_PRIOR_AGENTS` (dead reference once the inline path was removed)

## What's archived

Just the `master_room::task_spec_writer` agent:
- `app/assistant/agents/master_room/task_spec_writer/`
  - `config.yaml`, `agent_form.py`, `prompts/`

This agent was used only by the inline path; with that path gone, it has no callers.

## What stayed live

All shared infrastructure still serves the legitimate `task_spec_manager` flow:
- `task_spec::chat` agent (`app/assistant/agents/task_spec/chat/`)
- `task_spec_router_node` (`app/assistant/control_nodes/task_spec_router_node.py`)
- `task_create_final_router_node` (`app/assistant/control_nodes/task_create_final_router_node.py`)
- `task_spec_edit_apply_node`, `task_spec::editor`, etc.

`task_creation_done_tf` is still emitted by `task_spec::chat` and consumed by `task_create_final_router_node` — that contract is untouched. The router now serves only the task_spec_manager room instead of both.

## Restoration

Move `master_room::task_spec_writer` back to `app/assistant/agents/master_room/`, restore the deleted entries in `master_room_manager/config.yaml`, and re-add `master_room::task_spec_writer` to `_ACCEPTED_PRIOR_AGENTS`. Verify task_spec_manager still works (it should — its config wasn't touched).
