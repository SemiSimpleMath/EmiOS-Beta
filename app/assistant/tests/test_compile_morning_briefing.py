"""
Integration test: compile morning_briefing/task_spec.md through the real pipeline
and assert structural invariants the output IR must satisfy.

This burns real LLM time (~30-60s per run) and is non-deterministic, so it is
skipped by default. Run explicitly with:

    EMI_RUN_LLM_TESTS=1 .venv/Scripts/python.exe -m pytest \
        app/assistant/tests/test_compile_morning_briefing.py -v

The test compiles into a temporary task directory (``tasks/_compile_test_mb_<uuid>/``)
and cleans up on exit. It NEVER touches the real ``tasks/morning_briefing/``
artifacts.

The hand-crafted ``morning_briefing.json`` on disk is the informal reference
for what a correct compile looks like. These assertions encode that reference
as structural invariants:

  1. Executor selection (playwright_manager for web scraping; no fallback to
     emi_team_manager for browser work)
  2. Produces chain — the four data-gathering steps each produce an artifact,
     a synthesis step produces a summary, a save step persists it
  3. Consumes correctness — synthesis consumes the gather artifacts; save
     consumes the synthesis artifact; no orphan consumers
  4. Determinism classification — browser step is ``action``; fixed-tool steps
     (emails, todos, save) are ``tool_sequence``
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.assistant.tests.test_setup  # noqa: F401

SKIP_LLM = os.environ.get("EMI_RUN_LLM_TESTS") != "1"
pytestmark = pytest.mark.skipif(
    SKIP_LLM,
    reason="Set EMI_RUN_LLM_TESTS=1 to run LLM-based compile integration tests",
)


MORNING_BRIEFING_SPEC = REPO_ROOT / "tasks" / "morning_briefing" / "task_spec.md"


@pytest.fixture(scope="module")
def compiled_ir():
    """Compile morning_briefing once for the whole module; clean up on teardown.

    Module-scoped so all assertions share a single compile run (~60s) instead
    of seven. Each run is still non-deterministic; if one assertion fails due
    to LLM variance, the others still run on the same IR so the failure
    pattern is coherent.

    Note: the compile metadata node normalizes task_ids via strip("_"), so
    avoid leading/trailing underscores. Prefix ``mbtest_`` for easy manual
    cleanup if teardown fails.
    """
    task_id = f"mbtest_{uuid.uuid4().hex[:8]}"
    ir = _compile_spec_to_ir(task_id=task_id)
    yield ir

    # Opt-out of cleanup to inspect generated artifacts manually.
    if os.environ.get("EMI_KEEP_COMPILE_ARTIFACTS") == "1":
        print(f"\n[keep-artifacts] tasks/{task_id}/ preserved for inspection")
        return

    task_dir = REPO_ROOT / "tasks" / task_id
    if task_dir.exists():
        shutil.rmtree(task_dir, ignore_errors=True)
    try:
        from app.models.base import get_session
        from app.assistant.database.db_handler import UnifiedLog2026
        from sqlalchemy import delete

        with get_session() as session:
            session.execute(
                delete(UnifiedLog2026).where(UnifiedLog2026.id == f"spec::{task_id}")
            )
            session.commit()
    except Exception:
        pass


def _run_pre_compile(*, spec_markdown: str) -> str | None:
    """Run the same pre-compile the chat flow does (tool_planner agent → hints markdown).

    Mirrors ``TaskCreateFinalRouterNode._pre_compile`` but callable without the node.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.lib.task_utils.tool_planner import (
        build_tool_catalog_for_prompt,
        narrow_tools_for_task,
    )
    from app.assistant.utils.pydantic_classes import Message

    narrowed = narrow_tools_for_task(
        task_title="",
        task_goal="",
        step_descriptions=[spec_markdown[:500]],
    )
    catalog_text = build_tool_catalog_for_prompt(only_tools=narrowed)
    agent = DI.agent_factory.create_agent("master_room::tool_planner")
    agent.blackboard.update_state_value("agent_input_catalog", catalog_text)
    result = agent.action_handler(Message(agent_input=spec_markdown))

    data = result.data or {}
    step_plans = data.get("step_plans") or []
    if not step_plans:
        return None

    hints = "# Pre-compile hints (machine-generated)\n"
    for plan in step_plans:
        name = plan.get("step_name", "")
        kind = plan.get("kind", "")
        manager = plan.get("manager_name", "")
        tools = plan.get("tools", [])
        produces = plan.get("produces", [])
        consumes = plan.get("consumes", [])
        hints += f"\n## {name}\n"
        hints += f"- kind: {kind}\n"
        if manager:
            hints += f"- executor: {manager}\n"
        if tools:
            for t in tools:
                hints += f"- tool: {t.get('tool', '')}({t.get('args_json', '{}')})\n"
        if produces:
            for p in produces:
                hints += f"- produces: {p.get('id', '')} ({p.get('description', '')})\n"
        if consumes:
            hints += f"- consumes: {', '.join(consumes)}\n"
    return hints


def _compile_spec_to_ir(*, task_id: str) -> dict:
    """Run the full chat-flow-equivalent compile (pre-compile + compile) against the morning_briefing spec.

    Returns the parsed IR dict. Raises if the pipeline fails to produce one.
    """
    from app.assistant.lib.task_utils.task_create_compile_runner import (
        TaskCreateCompileRunner,
    )

    # Read the original spec body, prepend frontmatter with our test task_id so
    # the runner writes to tasks/<test_task_id>/ rather than auto-hashing.
    body = MORNING_BRIEFING_SPEC.read_text(encoding="utf-8")
    spec_with_frontmatter = (
        "---\n"
        f"task_id: {task_id}\n"
        "manager: emi_team_manager\n"
        "description: Integration test compile of morning briefing.\n"
        "inputs: []\n"
        "outputs: []\n"
        "idempotency: overwrite_outputs\n"
        "limits:\n"
        "  timeout_seconds: 600\n"
        "  max_cycles: 30\n"
        "---\n"
        + body
    )

    # Run pre-compile first (the hard step: tool planner assigns managers/tools/dataflow).
    hints = _run_pre_compile(spec_markdown=spec_with_frontmatter)

    TaskCreateCompileRunner.run(
        session_id=task_id,
        spec_markdown=spec_with_frontmatter,
        room_id="",
        precompile_hints=hints,
    )

    ir_path = REPO_ROOT / "tasks" / task_id / f"{task_id}.json"
    assert ir_path.exists(), f"Compile did not produce IR at {ir_path}"
    return json.loads(ir_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Invariant helpers
# ---------------------------------------------------------------------------

def _steps(ir: dict) -> list[dict]:
    steps = ir.get("steps")
    assert isinstance(steps, list) and steps, "IR must contain a non-empty steps list"
    return steps


def _step_by_executor(steps: list[dict], executor: str) -> list[dict]:
    return [s for s in steps if str(s.get("executor") or "") == executor]


def _steps_with_tool(steps: list[dict], tool_name_substring: str) -> list[dict]:
    hits = []
    for s in steps:
        for t in s.get("tools", []) or []:
            if isinstance(t, dict) and tool_name_substring in str(t.get("tool") or ""):
                hits.append(s)
                break
    return hits


def _all_consumes(steps: list[dict]) -> list[tuple[str, str]]:
    """Return (step_id, consumed_data_id) for every consume in any step."""
    out = []
    for s in steps:
        sid = str(s.get("id") or "")
        for did in s.get("consumes_data_ids", []) or []:
            out.append((sid, str(did)))
    return out


def _all_produces(steps: list[dict]) -> set[str]:
    produced: set[str] = set()
    for s in steps:
        for did in s.get("produces_data_ids", []) or []:
            produced.add(str(did))
    return produced


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_morning_briefing_compile_produces_ir(compiled_ir):
    """Smoke: the compile pipeline produces a non-empty IR for the spec."""
    assert compiled_ir.get("schema_version") == "task_ir_v1"
    steps = _steps(compiled_ir)
    assert len(steps) >= 4, f"Expected at least 4 steps, got {len(steps)}"


def test_morning_briefing_has_playwright_step_with_action_kind(compiled_ir):
    """Browser work must route to playwright_manager as an action step (not tool_sequence / not fallback)."""
    steps = _steps(compiled_ir)

    pw_steps = _step_by_executor(steps, "playwright_manager")
    assert pw_steps, (
        "No step with executor=playwright_manager. The spec explicitly names "
        "playwright_manager for cnn/bbc scraping; compile must preserve that."
    )
    for s in pw_steps:
        assert s.get("kind") == "action", (
            f"Playwright step {s.get('id')} has kind={s.get('kind')!r}; "
            f"browser work requires LLM-driven action, not tool_sequence."
        )

    # Anti-regression: browser work should not fall through to the generic fallback.
    for s in steps:
        if any(k in str(s.get("instruction", "")).lower() for k in ("cnn.com", "bbc.com", "navigate")):
            assert s.get("executor") != "emi_team_manager", (
                f"Browser step {s.get('id')} routed to emi_team_manager fallback; "
                f"should be playwright_manager."
            )


def test_morning_briefing_deterministic_steps_are_tool_sequence(compiled_ir):
    """Fixed-tool-call steps (emails, todos, save) must be kind=tool_sequence."""
    steps = _steps(compiled_ir)

    email_steps = _steps_with_tool(steps, "get_important_emails")
    assert email_steps, "No step calls get_important_emails."
    for s in email_steps:
        assert s.get("kind") == "tool_sequence", (
            f"Email step {s.get('id')} kind={s.get('kind')!r}; should be tool_sequence."
        )

    todo_steps = _steps_with_tool(steps, "get_todo_tasks")
    assert todo_steps, "No step calls get_todo_tasks."
    for s in todo_steps:
        assert s.get("kind") == "tool_sequence", (
            f"Todo step {s.get('id')} kind={s.get('kind')!r}; should be tool_sequence."
        )

    save_steps = _steps_with_tool(steps, "write_text_file") or _steps_with_tool(steps, "save_daily_summary")
    assert save_steps, "No save/write step found."
    for s in save_steps:
        assert s.get("kind") == "tool_sequence", (
            f"Save step {s.get('id')} kind={s.get('kind')!r}; should be tool_sequence."
        )


def test_morning_briefing_produces_chain(compiled_ir):
    """Each gather step produces exactly one artifact; a synthesis step produces one too."""
    steps = _steps(compiled_ir)
    produced = _all_produces(steps)

    # At minimum: one artifact from playwright, one from emails, one from todos,
    # one from synthesis. Weather may or may not be a distinct step.
    # Spec declares artifacts 1-6; we expect at least 4 produced artifacts.
    assert len(produced) >= 4, (
        f"Expected at least 4 produced artifacts across the pipeline, got {sorted(produced)}."
    )

    # Every produced id should look like artifact_N or fact_N.
    for pid in produced:
        assert pid.startswith("artifact_") or pid.startswith("fact_"), (
            f"Produced data id {pid!r} doesn't follow the artifact_*/fact_* convention."
        )


def test_morning_briefing_consumes_resolve(compiled_ir):
    """Every consumes_data_ids entry must be either produced upstream or preloaded."""
    steps = _steps(compiled_ir)
    produced = _all_produces(steps)
    preloaded = set(
        (compiled_ir.get("preloaded_task_state") or {}).get("artifacts", {}).keys()
    ) | set(
        (compiled_ir.get("preloaded_task_state") or {}).get("facts", {}).keys()
    )
    available = produced | preloaded

    orphans = [(sid, did) for (sid, did) in _all_consumes(steps) if did not in available]
    assert not orphans, (
        f"Orphan consumers (step consumes data that no step produces and isn't preloaded): {orphans}"
    )


def test_morning_briefing_synthesis_step_consumes_gather_artifacts(compiled_ir):
    """A synthesis step should consume multiple upstream gather artifacts."""
    steps = _steps(compiled_ir)

    # Find steps that consume >= 2 artifacts — that's the synthesis shape.
    synthesis_steps = [
        s for s in steps
        if len([d for d in (s.get("consumes_data_ids") or []) if str(d).startswith("artifact_")]) >= 2
    ]
    assert synthesis_steps, (
        "No step consumes 2+ artifacts. The briefing requires a synthesis step that "
        "combines headlines, emails, and todos into one summary."
    )


def test_morning_briefing_save_step_consumes_synthesis_output(compiled_ir):
    """A save step should consume exactly one artifact — the synthesis output."""
    steps = _steps(compiled_ir)

    save_steps = _steps_with_tool(steps, "write_text_file") or _steps_with_tool(steps, "save_daily_summary")
    assert save_steps, "No save step found."
    for s in save_steps:
        consumes = [d for d in (s.get("consumes_data_ids") or []) if str(d).startswith("artifact_")]
        assert len(consumes) == 1, (
            f"Save step {s.get('id')} consumes {consumes}; should consume exactly one "
            f"artifact (the synthesis output)."
        )
