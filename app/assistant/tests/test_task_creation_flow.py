"""
End-to-end-ish tests for the task creation subsystem.

Covers the seams where bugs have piled up:
  - TaskSpecRoom.restore_state / persist_state round-trip against unified_log
  - BLANK_SPEC fallback does not overwrite real content
  - Compile post_node seeds frontmatter `inputs:` into preloaded_task_state
  - Compile runner writes clean spec to disk (no hint pollution)

These are unit-ish: they do NOT run LLMs. They exercise the deterministic
plumbing around the LLM calls, which is where every bug we've found lives.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

# Ensure repo root on sys.path.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.lib.task_utils.task_spec_store import (
    load_task_spec_draft,
    make_room_id,
    save_task_spec_draft,
)
from app.assistant.room_handlers.task_spec_room import BLANK_SPEC, TaskSpecRoom


def _unique_task_id() -> str:
    return f"testtask_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# unified_log round-trip
# ---------------------------------------------------------------------------

def test_save_and_load_task_spec_round_trip():
    """save_task_spec_draft → load_task_spec_draft preserves spec content."""
    task_id = _unique_task_id()
    spec_md = "---\ntask_id: " + task_id + "\n---\n# Description\nround-trip\n"
    save_task_spec_draft(task_id, spec_markdown=spec_md, caller="test")

    loaded = load_task_spec_draft(task_id)
    assert loaded is not None
    assert loaded["spec_markdown"] == spec_md
    assert loaded["room_id"] == make_room_id(task_id)


# ---------------------------------------------------------------------------
# TaskSpecRoom: restore_state
# ---------------------------------------------------------------------------

def test_restore_state_populates_blackboard_with_saved_spec():
    """TaskSpecRoom.restore_state reads unified_log and writes to blackboard."""
    task_id = _unique_task_id()
    spec_md = "# Description\nhello-restore\n"
    save_task_spec_draft(task_id, spec_markdown=spec_md, caller="test")

    bb = Blackboard()
    room = TaskSpecRoom(room_id=f"task_spec::{task_id}")
    room.restore_state(blackboard=bb, session_id=task_id)

    assert bb.get_state_value("spec") == spec_md


def test_restore_state_sets_both_spec_and_current_spec_on_success():
    """
    Bug #5 (audit): restore_state only sets current_spec in the blank fallback,
    not on the happy path. Downstream code that reads `current_spec` sees
    stale/empty state after load.
    """
    task_id = _unique_task_id()
    spec_md = "# Description\nhello-current-spec\n"
    save_task_spec_draft(task_id, spec_markdown=spec_md, caller="test")

    bb = Blackboard()
    room = TaskSpecRoom(room_id=f"task_spec::{task_id}")
    room.restore_state(blackboard=bb, session_id=task_id)

    # Expected: both keys carry the loaded spec.
    assert bb.get_state_value("spec") == spec_md
    # EXPECTED TO FAIL pre-fix — restore_state only sets current_spec in the
    # blank fallback branch, so this assertion surfaces the bug.
    assert bb.get_state_value("current_spec") == spec_md


# ---------------------------------------------------------------------------
# TaskSpecRoom: persist_state
# ---------------------------------------------------------------------------

def test_persist_state_writes_blackboard_spec_to_unified_log():
    """persist_state pushes blackboard 'spec' back into unified_log."""
    task_id = _unique_task_id()
    # Seed an initial draft so the row exists.
    save_task_spec_draft(task_id, spec_markdown="initial\n", caller="test")

    bb = Blackboard()
    bb.update_state_value("spec", "edited content\n")

    room = TaskSpecRoom(room_id=f"task_spec::{task_id}")
    room.persist_state(blackboard=bb, session_id=task_id)

    reloaded = load_task_spec_draft(task_id)
    assert reloaded is not None
    assert reloaded["spec_markdown"] == "edited content\n"


# ---------------------------------------------------------------------------
# BLANK_SPEC must not clobber real content
# ---------------------------------------------------------------------------

def test_restore_state_does_not_overwrite_blackboard_with_blank_on_missing_draft():
    """
    Bug #4 (audit): if the draft isn't in unified_log (e.g. fresh session,
    content only on disk, or a race), restore_state writes BLANK_SPEC to the
    blackboard. A later persist_state would then save the blank over the real
    content.

    Correct behavior: if there's nothing to restore, leave the blackboard
    'spec' untouched (or at minimum, don't clobber an existing non-empty
    value already on the blackboard).
    """
    bb = Blackboard()
    bb.update_state_value("spec", "existing content from an earlier turn\n")

    task_id = _unique_task_id()  # nothing saved for this id
    room = TaskSpecRoom(room_id=f"task_spec::{task_id}")
    room.restore_state(blackboard=bb, session_id=task_id)

    # EXPECTED TO FAIL pre-fix: restore_state writes BLANK_SPEC unconditionally
    # on the miss path. This assertion locks in the correct behavior.
    current = bb.get_state_value("spec")
    assert current != BLANK_SPEC, (
        "restore_state clobbered existing blackboard spec with BLANK_SPEC "
        "when the unified_log had no draft."
    )


# ---------------------------------------------------------------------------
# Compile post_node: frontmatter `inputs:` → preloaded_task_state.artifacts
# ---------------------------------------------------------------------------

def test_frontmatter_inputs_also_visible_to_orphan_strip_check():
    """
    Regression: the orphan-strip check and the preloaded-state builder MUST
    agree on what counts as preloaded. Earlier, _extract_preloaded_task_state
    read frontmatter but _preloaded_data_ids_from_compile_seed did not — so
    the orphan-strip fired first, stripped the consume, and the preloaded
    state was only populated AFTER the damage was done. Both code paths now
    share _declarations_from_frontmatter_dict.
    """
    from app.assistant.control_nodes.task_compile_post_node import (
        TaskCompilePostNode,
    )
    frontmatter = {
        "inputs": [{"id": "artifact_1", "description": "Spammer Registry Google Doc"}],
    }
    decls = TaskCompilePostNode._declarations_from_frontmatter_dict(frontmatter)
    assert "artifact_1" in decls


def test_post_node_seeds_frontmatter_inputs_into_preloaded_task_state():
    """
    Bug #3 (audit): spec frontmatter `inputs: - id: artifact_1` never seeds
    preloaded_task_state.artifacts. The post-node's orphan-strip then removes
    any step consuming `artifact_1` because it can't find it in preloaded state
    OR in any upstream producer — even though the spec declared it as a task
    input.
    """
    from app.assistant.control_nodes.task_compile_post_node import (
        TaskCompilePostNode,
    )

    # Minimal fake IR with a step that consumes artifact_1.
    # If the post-node correctly reads frontmatter inputs, artifact_1 should
    # land in preloaded_task_state.artifacts and the consume survives.
    frontmatter = {
        "task_id": "testtask",
        "inputs": [
            {"id": "artifact_1", "description": "Pre-provided input doc"},
        ],
        "outputs": [],
    }
    compiled_task = {
        "schema_version": "task_ir_v1",
        "entry_step_id": "step_1",
        "steps": [
            {
                "id": "step_1",
                "kind": "action",
                "executor": "emi_team_manager",
                "instruction": "do work",
                "consumes_data_ids": ["artifact_1"],
                "produces_data_ids": [],
                "next_step": "step_2",
            },
            {
                "id": "step_2",
                "kind": "end",
                "consumes_data_ids": [],
                "produces_data_ids": [],
            },
        ],
        "preloaded_task_state": {"facts": {}, "artifacts": {}, "flags": {}},
    }

    # Call the node's static preload-extraction helper if it exists, or
    # exercise the whole post-node pipeline. We don't want to instantiate the
    # full manager, so prefer the helper if accessible.
    seed_fn = getattr(TaskCompilePostNode, "_seed_preloaded_from_frontmatter", None)
    if seed_fn is None:
        pytest.skip(
            "TaskCompilePostNode has no _seed_preloaded_from_frontmatter helper "
            "yet — this test is the spec for the fix."
        )
    seed_fn(compiled_task=compiled_task, frontmatter=frontmatter)

    artifacts = compiled_task["preloaded_task_state"]["artifacts"]
    assert "artifact_1" in artifacts, (
        "Frontmatter inputs.id=artifact_1 should be seeded into "
        "preloaded_task_state.artifacts so the consume on step_1 isn't stripped."
    )


# ---------------------------------------------------------------------------
# Compile runner: task_spec.md stays clean (no hint pollution)
# ---------------------------------------------------------------------------

def test_compile_runner_writes_clean_spec_to_task_spec_md(tmp_path, monkeypatch):
    """
    Regression: an earlier bug wrote `spec + precompile_hints` to task_spec.md,
    so a subsequent load re-fed polluted content to the compiler. The fix
    keeps task_spec.md as spec-only; hints live in sibling pre-compile.md.
    """
    from app.assistant.lib.task_utils import task_create_compile_runner as tccr

    # Redirect repo_root + _TASKS_ROOT to a temp dir so we don't touch the real tree.
    monkeypatch.setattr(tccr, "get_repo_root", lambda: str(tmp_path))

    task_id = _unique_task_id()
    spec_md = (
        "---\ntask_id: " + task_id + "\n---\n"
        "# Description\nclean-spec-test\n"
    )
    hints = "# Pre-compile hints (machine-generated)\n\n## Step\n- executor: emi_team_manager\n"

    spec_path = tccr.TaskCreateCompileRunner._write_spec_to_disk(
        session_id="sess_test",
        spec_markdown=spec_md,
        spec_dict=None,
        precompile_hints=hints,
    )

    on_disk = Path(spec_path).read_text(encoding="utf-8")
    assert on_disk == spec_md, (
        "task_spec.md must be the clean spec only — no pre-compile hints "
        "appended. Hints belong in the sibling pre-compile.md."
    )

    precompile_md = Path(spec_path).parent / "pre-compile.md"
    assert precompile_md.exists()
    assert "Pre-compile hints" in precompile_md.read_text(encoding="utf-8")


def main() -> int:
    # Allow running as a plain script for the "bootstrap DI before imports" convention.
    test_save_and_load_task_spec_round_trip()
    test_restore_state_populates_blackboard_with_saved_spec()
    test_restore_state_sets_both_spec_and_current_spec_on_success()
    test_persist_state_writes_blackboard_spec_to_unified_log()
    test_restore_state_does_not_overwrite_blackboard_with_blank_on_missing_draft()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
