# Room-Scoped Persistence for Task Specs and Documents

## Problem

Task spec and doc editing rooms lose state across server restarts because they rely on:
- Global blackboard (in-memory, ephemeral)
- Session service (stored on global blackboard, also ephemeral)

## Solution

Use unified_log as the persistence layer, scoped by room_id. Each task spec or doc gets its own room_id. The spec/doc content lives as a Message in unified_log alongside the conversation history.

## Design

### Room ID as Identity

Each editing session gets a unique room_id:
- Task spec: `task_spec::{task_id}` (e.g., `task_spec::spammer_finder`)
- Doc editor: `doc_editor::{doc_id}` (e.g., `doc_editor::1ABC123XYZ`)

The room_id IS the identity. No separate session_id needed.

### Spec/Doc as a Message

The document content is stored as a special Message in unified_log:

```python
Message(
    id=f"spec::{task_id}",           # stable upsert key
    source="task_spec_content",       # distinguishes from chat messages
    room_id=f"task_spec::{task_id}",
    content=spec_markdown,
    metadata={
        "task_id": task_id,
        "doc_type": "md",
        "updated_at_utc": now_utc.isoformat(),
        "editor_watermark": 42,       # line count for editor gating
    },
)
```

For Google Docs:
```python
Message(
    id=f"doc::{doc_id}",
    source="doc_draft_content",
    room_id=f"doc_editor::{doc_id}",
    content=doc_markdown,
    metadata={
        "doc_id": doc_id,
        "doc_type": "gdoc",
        "doc_name": title,
        "doc_url": url,
        "updated_at_utc": now_utc.isoformat(),
        "editor_watermark": 0,
    },
)
```

### Load Flow

On page load / room entry:

1. Query unified_log for `source='task_spec_content'` WHERE `room_id = 'task_spec::{task_id}'` → get the spec content
2. Query unified_log for chat messages WHERE `room_id = 'task_spec::{task_id}'` → get conversation history
3. Both land on the blackboard via the standard room history seeding mechanism

The room handler's `restore_state()` reads the spec message from unified_log and puts it on the blackboard.

### Save Flow

After every successful edit:

1. Upsert the spec/doc Message to unified_log (same id, updated content + metadata)
2. Chat messages are already persisted by the room session manager's standard outbound persistence

### Resume Flow

User opens `http://localhost:8000/task/create?load=spammer_finder`:

1. Page JS sends first message to `room_id=task_spec::spammer_finder`
2. Room session manager loads history from unified_log for that room_id
3. Spec message (`source='task_spec_content'`) is loaded and put on blackboard as `spec`
4. Chat messages are loaded as conversation history
5. User picks up exactly where they left off — spec visible, full chat history available

### What Changes

**Room handler** (`TaskSpecRoom`, `DocEditorRoom`):
- `ensure_session()` → just uses the room_id, no session service
- `restore_state()` → queries unified_log for the spec/doc content message
- `persist_state()` → upserts the spec/doc message to unified_log

**Load endpoints** (`/task-graph/spec/load`, `/doc/load-gdoc`):
- Write the spec/doc content as a Message to unified_log
- Set room_id appropriately

**Pre-nodes** (`task_spec_router_node`, `doc_writer_pre_node`):
- Read spec from local blackboard (already seeded by room handler)
- No global blackboard reads needed

**Post-nodes** (`task_spec_edit_apply_node`, `doc_create_final_router_node`):
- Upsert the edited spec/doc back to unified_log
- No global blackboard writes needed

**What goes away**:
- `TaskCreationSessionService` (for task specs)
- `DocCreationSessionService` (for docs)
- Global blackboard flat key writes (`spec`, `doc_markdown_last_emitted`, etc.)
- Session-based watermark persistence

### Migration

Existing tasks on disk (`tasks/*/task_spec.md`) remain the source of truth for compiled tasks. The unified_log stores the editing state — the in-progress draft. When the user says "compile", the spec is written to disk AND unified_log.

### Page URL Pattern

- New task: `/task/create` → creates room `task_spec::task_{uuid}`
- Load existing: `/task/create?load=spammer_finder` → opens room `task_spec::spammer_finder`
- Resume editing: same URL → loads from unified_log
- New doc: `/doc/editor` → creates room `doc_editor::doc_{uuid}`
- Load gdoc: `/doc/editor?gdoc=Spammer+Registry` → opens room `doc_editor::{gdoc_id}`
