"""Runtime state must live on the data dir, never in the image (2026-09-11).

FOUR separate outages in one day, all the same bug: a path derived from the
repo root instead of the data dir. Under EMI_DATA_DIR (how the container runs)
get_repo_root() is the IMAGE, so state written there is invisible to every
reader that resolves via get_data_dir()/get_resources_dir() -- and is destroyed
by the next rebuild.

  - belief export wrote /app/resources/kg_derived, readers looked in
    /data/resources/kg_derived  -> "Beliefs file not found" on every dayflow
    run, routines silently ran with no belief context
  - chat-memory vectors persisted into the image
  - camera snapshot UI scanned /app/data/ring_snapshots
  - (previously, T-469) Personalize step_configs edits wiped by each rebuild

The rule these tests pin: a module that touches RUNTIME STATE resolves it
through the path_utils helpers, which honour EMI_DATA_DIR. get_repo_root() is
for CODE (templates shipped in the image, package data), not for state.

BASELINE-LOCKED. There are known remaining offenders; this does not fail on
them. It fails when a NEW one appears, and when a listed one is fixed it must
be removed from the baseline (the test enforces that too, so the list cannot
rot into a permanent excuse).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]

# Dirs that hold runtime state. Joining one of these onto a repo-root-derived
# path is the bug. "templates" and "static" are deliberately absent -- those
# ARE code and belong in the image.
_STATE_DIRS = ("data", "resources", "configs", "logs", "uploads", "chroma")

# repo-root-ish expressions, then a state dir
_PATTERN = re.compile(
    r"(?:get_repo_root\(\)|get_app_root\(\)|Path\(__file__\)\.resolve\(\)\.parents\[\d\])"
    r"\s*/\s*[\"'](" + "|".join(_STATE_DIRS) + r")[\"']"
)

_SEARCH_ROOTS = ("app", "belief_engine", "belief_engine_v2", "work_objects")

# Known offenders as of 2026-09-11. Each entry is "<relpath>::<count>".
# Shrink this list as they are fixed; never grow it.
_BASELINE = {
    "app/bootstrap.py",
    "app/routes/kg_interests.py",
    "app/services/llm_call_logger.py",
    "app/services/llm_factory.py",
    "app/assistant/slack_interface/slack_room_config.py",
    "app/assistant/routine_handlers/ring_camera_motion_poll.py",
    "app/me/layout.py",
    "app/assistant/dj_manager/music_dataset.py",
    "app/assistant/pod_store/file_ingest.py",
    "app/assistant/pod_store/resolvers.py",
    "app/assistant/pod_store/image_ingest.py",
    "belief_engine/tagging.py",
    "app/assistant/utils/path_utils.py",  # the helpers themselves: correct by definition
    "app/assistant/lib/tools/send_email/allowlist.py",
    "app/assistant/lib/tools/local_camera_snapshot/local_camera_snapshot.py",
    "belief_engine/state/sweep_tracker.py",
    "app/assistant/agent_runtime/services/llm_client.py",
    "app/assistant/lib/tools/claude_code_invoke/session_store.py",
    # belief_engine_v2 is NOT shipped (absent from the Dockerfile COPY list,
    # verified missing from the running image) and nothing outside it imports
    # it. Inert, so baselined rather than edited -- changing unshipped, untested
    # code to satisfy a lint is risk without benefit. If it is ever added to the
    # image, fix these first.
    "belief_engine_v2/export.py",
    "belief_engine_v2/tags.py",
}


def _offenders() -> set[str]:
    found: set[str] = set()
    for root in _SEARCH_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(REPO).as_posix()
            if "_archived" in rel or "/tests/" in rel:
                continue
            try:
                text = py.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # ignore the pattern inside comments -- those are explanations of
            # the bug, which several fixes deliberately leave behind
            code = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
            if _PATTERN.search(code):
                found.add(rel)
    return found


def test_no_new_repo_root_state_paths():
    """A NEW module must not address runtime state relative to the repo root."""
    new = _offenders() - _BASELINE
    assert not new, (
        "These files derive a runtime-state path from the repo root, which is the "
        "IMAGE under EMI_DATA_DIR. Use get_data_dir()/get_resources_dir()/"
        "get_configs_dir() instead:\n  " + "\n  ".join(sorted(new))
    )


def test_baseline_has_no_stale_entries():
    """A fixed file must be removed from the baseline, so the list cannot rot."""
    stale = _BASELINE - _offenders()
    assert not stale, (
        "These files no longer offend and must be dropped from _BASELINE:\n  "
        + "\n  ".join(sorted(stale))
    )


def test_the_four_2026_09_11_regressions_stay_fixed():
    """Named guards for the four that actually broke production."""
    checks = {
        "belief_engine/export/export_beliefs.py": "get_resources_dir()",
        "app/assistant/agent_runtime/services/chat_memory_rag.py": "get_data_dir()",
        "app/routes/preferences.py": "get_data_dir()",
    }
    for rel, expected in checks.items():
        text = (REPO / rel).read_text(encoding="utf-8")
        assert expected in text, f"{rel} must resolve state via {expected}"
