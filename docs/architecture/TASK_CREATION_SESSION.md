# Task Creation Session Lifecycle

## Overview

Task creation uses a session to track state between the frontend and backend. The session ID is the isolation boundary — it prevents spec edits from one task leaking into another.

## State flow

- **Disk** is the source of truth for saved specs (`tasks/<task_id>/task_spec.md`).
- **Blackboard** holds the live spec during a session as `spec`.
- **Session ID** ties frontend messages to backend state.

## Lifecycle

### New session

1. Backend creates a fresh session ID.
2. Blank spec written to blackboard as `spec`.
3. Session ID returned to frontend.
4. Frontend stores it and attaches it to every chat message.

### Load existing task

1. Backend creates a fresh session ID.
2. Spec read from disk (`tasks/<task_id>/task_spec.md`).
3. Spec written to blackboard as `spec`.
4. Session ID returned to frontend.
5. Frontend switches to new session ID.

### Edit during session

1. Chat agent converses with user, sets `update_spec_tf` when content should update the spec.
2. Router node puts current `spec` and recent exchanges on the blackboard, routes to editor agent.
3. Editor agent produces search/replace edits.
4. Edit apply node applies edits to the spec, writes updated spec back to blackboard (`spec`) and back to disk.

### End session / switch task

1. Spec is already on disk (edit apply node writes after every edit).
2. Clear blackboard state and session ID.
3. If loading a different task, start a new session (see "Load existing task").

## Key rules

- Starting or loading always creates a **new session ID**. Old session is abandoned.
- The frontend must send `task_creation_session_id` with every chat message.
- The backend owns the spec. The frontend displays it but never sends it back.
- The blackboard key for the spec is `spec`. Agents access it via `{{ spec }}` in prompts.
- Every edit is persisted to disk immediately. There is no "unsaved" state.
