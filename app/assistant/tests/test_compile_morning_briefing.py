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

  1. Executor selection (playwright_manager for web scraping; browser work stays
     on playwright_manager, not the generic emi_team_manager)
  2. Produces chain — the four data-gathering steps each produce an artifact,
     a synthesis step produces a summary, a save step persists it
  3. Consumes correctness — synthesis consumes the gather artifacts; save
     consumes the synthesis artifact; no orphan consumers
  4. Determinism classification — browser step is an ``action`` node; fixed-tool
     steps (emails, todos, save) are ``tool`` nodes

The compiler emits a work object directly (driver=task_runner): steps are ``nodes``
and dataflow (``consumes``) is expressed as ``depends_on`` edges rather than per-step
id lists, so the consume invariants below are checked via incoming-edge counts.
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
MORNING_BRIEFING_SPEC = REPO_ROOT / "tasks" / "morning_briefing" / "task_spec.md"
pytestmark = [
    pytest.mark.skipif(
        SKIP_LLM,
        reason="Set EMI_RUN_LLM_TESTS=1 to run LLM-based compile integration tests",
    ),
    pytest.mark.skipif(
        not MORNING_BRIEFING_SPEC.exists(),
        reason="tasks/morning_briefing/task_spec.md is gitignored/local; skip when the spec is absent",
    ),
]


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

def _nodes(wo: dict) -> list[dict]:
    nodes = wo.get("nodes")
    assert isinstance(nodes, list) and nodes, "work object must contain a non-empty nodes list"
    return nodes


def _payload(node: dict) -> dict:
    payload = node.get("payload")
    return payload if isinstance(payload, dict) else {}


def _nodes_by_executor(nodes: list[dict], executor: str) -> list[dict]:
    return [n for n in nodes if str(_payload(n).get("executor") or "") == executor]


def _nodes_with_tool(nodes: list[dict], tool_name_substring: str) -> list[dict]:
    hits = []
    for n in nodes:
        for t in _payload(n).get("tools", []) or []:
            if isinstance(t, dict) and tool_name_substring in str(t.get("tool") or ""):
                hits.append(n)
                break
    return hits


def _all_produces(nodes: list[dict]) -> set[str]:
    produced: set[str] = set()
    for n in nodes:
        for did in _payload(n).get("produces", []) or []:
            produced.add(str(did))
    return produced


def _incoming_depends_on(wo: dict) -> dict[str, int]:
    """dst node id -> count of depends_on edges pointing at it (how many upstream producers it consumes)."""
    counts: dict[str, int] = {}
    for edge in wo.get("edges", []) or []:
        if str(edge.get("relation") or "") == "depends_on":
            dst = str(edge.get("dst") or "")
            counts[dst] = counts.get(dst, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_morning_briefing_compile_produces_work_object(compiled_ir):
    """Smoke: the compile pipeline produces a non-empty work object for the spec."""
    assert compiled_ir.get("driver") == "task_runner"
    nodes = _nodes(compiled_ir)
    assert len(nodes) >= 4, f"Expected at least 4 nodes, got {len(nodes)}"


def test_morning_briefing_has_playwright_action_node(compiled_ir):
    """Browser work must route to playwright_manager as an action node (not a tool node)."""
    nodes = _nodes(compiled_ir)

    pw_nodes = _nodes_by_executor(nodes, "playwright_manager")
    assert pw_nodes, (
        "No node with executor=playwright_manager. The spec explicitly names "
        "playwright_manager for cnn/bbc scraping; compile must preserve that."
    )
    for n in pw_nodes:
        assert n.get("type") == "action", (
            f"Playwright node {n.get('id')} has type={n.get('type')!r}; "
            f"browser work requires an LLM-driven action node, not a tool node."
        )

    # Anti-regression: browser work must stay on playwright_manager, not the generic emi_team_manager.
    for n in nodes:
        instruction = str(_payload(n).get("instruction") or n.get("content") or "").lower()
        if any(k in instruction for k in ("cnn.com", "bbc.com", "navigate")):
            assert _payload(n).get("executor") != "emi_team_manager", (
                f"Browser node {n.get('id')} routed to the generic emi_team_manager; "
                f"should be playwright_manager."
            )


def test_morning_briefing_deterministic_steps_are_tool_nodes(compiled_ir):
    """Fixed-tool-call steps (emails, todos, save) must be tool nodes."""
    nodes = _nodes(compiled_ir)

    for tool_name in ("get_important_emails", "get_todo_tasks"):
        tool_nodes = _nodes_with_tool(nodes, tool_name)
        assert tool_nodes, f"No node calls {tool_name}."
        for n in tool_nodes:
            assert n.get("type") == "tool", (
                f"{tool_name} node {n.get('id')} type={n.get('type')!r}; should be a tool node."
            )

    save_nodes = _nodes_with_tool(nodes, "write_text_file") or _nodes_with_tool(nodes, "save_daily_summary")
    assert save_nodes, "No save/write node found."
    for n in save_nodes:
        assert n.get("type") == "tool", (
            f"Save node {n.get('id')} type={n.get('type')!r}; should be a tool node."
        )


def test_morning_briefing_produces_chain(compiled_ir):
    """The gather + synthesis nodes each declare produced artifacts."""
    nodes = _nodes(compiled_ir)
    produced = _all_produces(nodes)

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


def test_morning_briefing_edges_resolve(compiled_ir):
    """Every depends_on edge connects two real nodes — the WO analogue of 'no orphan consumers'."""
    nodes = _nodes(compiled_ir)
    node_ids = {str(n.get("id") or "") for n in nodes}
    dangling = [
        (str(edge.get("src") or ""), str(edge.get("dst") or ""))
        for edge in compiled_ir.get("edges", []) or []
        if str(edge.get("relation") or "") == "depends_on"
        and (str(edge.get("src") or "") not in node_ids or str(edge.get("dst") or "") not in node_ids)
    ]
    assert not dangling, (
        f"Dangling depends_on edges (an endpoint is not a node): {dangling}"
    )


def test_morning_briefing_synthesis_node_consumes_multiple_producers(compiled_ir):
    """A synthesis node should depend on multiple upstream gather nodes (2+ incoming depends_on edges)."""
    nodes = _nodes(compiled_ir)
    end_ids = {str(n.get("id") or "") for n in nodes if _payload(n).get("is_end")}
    incoming = _incoming_depends_on(compiled_ir)

    synthesis = [nid for nid, count in incoming.items() if count >= 2 and nid not in end_ids]
    assert synthesis, (
        "No work node depends on 2+ producers. The briefing requires a synthesis node "
        "that combines headlines, emails, and todos into one summary."
    )


def test_morning_briefing_save_node_consumes_synthesis_output(compiled_ir):
    """A save node should depend on exactly one upstream node — the synthesis output."""
    nodes = _nodes(compiled_ir)
    incoming = _incoming_depends_on(compiled_ir)

    save_nodes = _nodes_with_tool(nodes, "write_text_file") or _nodes_with_tool(nodes, "save_daily_summary")
    assert save_nodes, "No save node found."
    for n in save_nodes:
        nid = str(n.get("id") or "")
        assert incoming.get(nid, 0) == 1, (
            f"Save node {nid} has {incoming.get(nid, 0)} incoming depends_on edges; "
            f"should depend on exactly one (the synthesis output)."
        )
