"""Offline contract checks for strategic versus execution views of the same graph."""
from datetime import datetime, timezone
from work_objects.model import WorkObject, WorkNode
from app.assistant.dayflow_orchestrator.work_context import work_data, worker_data, render_view


def complex_work():
    wo = WorkObject(id="work_release_example", title="Prepare the shared application release", goal_node_id="goal",
                    constraints={"success_criteria": "Preserve existing users data; obtain approval before publishing."})
    wo.add_node(WorkNode(id="goal", work_id=wo.id, type="goal", content="Prepare a reliable application update and present the release decision to the user.", payload={"goal_unmet_attempts": 2}))
    specifications = [
        ("audit", "closed", "Read the orchestrator end to end"),
        ("repair", "proposed", "Repair the dispatch race using the saved investigation"),
        ("verify", "done", "Verify the result and judgment lifecycle"),
        ("docs", "actionable", "Update developer and coding agent documentation"),
        ("compat", "dispatched", "Check an existing user database copy"),
        ("review", "waiting", "Present the release assessment at the agreed review time"),
        ("permission", "failed", "Obtain access to the deployment environment"),
        ("old_plan", "abandoned", "Use the retired recovery implementation"),
        ("old_docs", "superseded", "Publish the preliminary documentation"),
    ]
    for nid, status, title in specifications:
        wo.add_node(WorkNode(id=nid, work_id=wo.id, type="subtask", parent_id="goal", title=title,
                            content=title + ". Preserve all findings and explain any remaining limitations.",
                            status=status, satisfied_when_kind="tool_success", payload={"dispatch_epoch": 1}))
    wo.nodes["audit"].payload.update(finalized_epoch=1, finalizer={"verdict": "achieved", "dispatch_epoch": 1,
        "outcome": "Traced claim, execution, result persistence and judgment; documented the race."})
    wo.nodes["repair"].payload.update(finalized_epoch=1, failure_count=1, finalizer={"verdict": "retry", "dispatch_epoch": 1,
        "outcome": "The first change still allowed two workers to claim the same task.",
        "recommendation": "Make the claim atomic, then repeat the concurrency check.", "next_step": "new_approach"})
    wo.nodes["permission"].payload.update(finalized_epoch=1, failure_count=1, finalizer={"verdict": "unrecoverable", "dispatch_epoch": 1,
        "outcome": "The deployment credentials are unavailable.", "next_step": "ask_user",
        "question_for_user": "Can you provide access or defer deployment?", "recommendation": "Ask once and wait."})
    for nid in ("old_plan", "old_docs"):
        wo.nodes[nid].payload["terminal"] = {"reason": "Replaced by the agreed atomic lifecycle."}
    wo.nodes["review"].wake_kind = "time"
    wo.nodes["review"].wake_at = datetime(2026, 9, 20, 18, tzinfo=timezone.utc)
    wo.add_edge("audit", "repair", "depends_on")
    wo.add_edge("verify", "review", "depends_on")
    wo.add_edge("docs", "review", "depends_on")
    wo.add_node(WorkNode(id="private_helper", work_id=wo.id, type="subtask", parent_id="repair", status="done",
                        title="INTERNAL_PROVENANCE_ONLY", content="Saved the reproduction; reuse it rather than starting over."))
    wo.add_node(WorkNode(id="private_evidence", work_id=wo.id, type="evidence", parent_id="private_helper", status="verified",
                        content="RAW_INTERNAL_EVIDENCE", pod_ref="datapod:synthetic-reproduction"))
    return wo


def test_strategic_projection_has_complete_decision_context_without_helpers():
    wo = complex_work()
    text = render_view("portfolio_list", works=[work_data(wo)])
    for nid, node in wo.nodes.items():
        if wo.is_work_unit(node):
            assert f"[{node.status}] {nid}" in text
            assert node.content in text
    assert "RAW_INTERNAL_EVIDENCE" not in text
    assert "INTERNAL_PROVENANCE_ONLY" not in text
    assert "first change still allowed two workers" in text
    assert "pending architect revision" in text
    assert "verify [done; BLOCKED]" in text
    assert "audit [closed; satisfied]" in text
    assert "AWAITING JUDGMENT" in text
    assert "goal failures judged by finalizer: 2" in text
    assert "Preserve existing users data" in text
    assert "Can you provide access" in text


def test_takeover_and_finalizer_view_preserves_owned_history():
    view = worker_data(complex_work(), "repair")
    text = render_view("worker", view=view)
    assert "RAW_INTERNAL_EVIDENCE" in text
    assert "INTERNAL_PROVENANCE_ONLY" in text
    assert "datapod:synthetic-reproduction" in text
    assert "parent:private_helper" in text
    assert "reuse it rather than starting over" in text


def test_consumed_instruction_is_identified_and_empty_portfolio_renders():
    wo = complex_work()
    for node in wo.nodes.values():
        if node.payload.get("finalizer", {}).get("next_step"):
            node.payload["finalizer"]["consumed_at"] = "2026-09-19T12:00:00Z"
    view = work_data(wo)
    assert not view["pending_replan"]
    assert "instruction applied" in render_view("portfolio", work=view)
    assert "(no active work objects)" in render_view("portfolio_list", works=[])


def test_active_strategic_renderers_share_statuses_and_hide_terminal_wakes():
    from app.assistant.control_nodes.work_architect_node import _render_existing_graph
    from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio
    wo = complex_work()
    wo.nodes["old_plan"].wake_kind = "signal"
    wo.nodes["old_plan"].wake_ref = "RETIRED_WAKE_MUST_NOT_APPEAR"
    for render in (_render_existing_graph, render_work_portfolio):
        text = render(wo)
        assert "RETIRED_WAKE_MUST_NOT_APPEAR" not in text
        assert "RAW_INTERNAL_EVIDENCE" not in text
        assert "verify [done; BLOCKED]" in text
        assert "2 ATTEMPTS HAVE NOT ACHIEVED THIS GOAL" in text
        assert "first change still allowed two workers" in text


def test_render_actual_architect_and_steward_templates(monkeypatch, tmp_path):
    """Capture the real replan Message and render both agents with the production Jinja environment."""
    import os
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock
    import yaml
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.control_nodes.work_architect_node import WorkArchitectNode
    from app.assistant.dayflow_orchestrator import work_store, work_architect_apply
    from app.assistant.dayflow_orchestrator.work_portfolio import render_portfolio
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
    from app.assistant.tests.dayflow.conftest import FakeBlackboard

    wo = complex_work()
    captured = []
    store = SimpleNamespace(list_work_objects=lambda: [{"id": wo.id, "status": "active"}],
                            load=lambda wid: wo, apply=Mock())
    monkeypatch.setattr(work_store, "get_dayflow_work_store", lambda: store)
    monkeypatch.setattr(work_architect_apply, "apply_architect_dag", lambda *a, **kw: {})
    monkeypatch.setattr(DI, "agent_factory", SimpleNamespace(create_agent=lambda *a, **kw:
        SimpleNamespace(action_handler=lambda msg: captured.append(msg) or SimpleNamespace(data={}))))
    bb = FakeBlackboard({"replan_work_ids": [wo.id], "work_portfolio": render_portfolio([wo]),
        "recent_responded_tickets": {"acknowledged": [{"title": "Release timing", "user_comment": "Wait for my approval before publishing."}]}})
    node = WorkArchitectNode(name="architect", blackboard=bb, agent_registry={}, tool_registry={})
    monkeypatch.setattr(node, "_scope", lambda msg: None)
    node.action_handler(SimpleNamespace())
    assert len(captured) == 1
    assert "ACKNOWLEDGED" in captured[0].information
    assert "Wait for my approval before publishing." in captured[0].information
    root = Path(__file__).resolve().parents[2] / "agents" / "dayflow_orchestrator"
    artifact = ["# Complex work object: architect and steward prompt example",
        "Synthetic data only. Generated from the current agent Jinja templates using the production Jinja environment.",
        "The architect input is captured from its actual replan control node; store writes and the LLM are mocked. The steward portfolio uses its active renderer. Optional personal resources are empty.",
        "This is a prompt inspection artifact, not evidence that deployment or all orchestration repairs are complete."]
    for name, overrides in [
        ("work_architect", {"task": captured[0].task, "information": captured[0].information}),
        ("strategic_planner_wo", {"work_portfolio": render_portfolio([wo]), "recent_completed_work": ""}),
    ]:
        config = yaml.safe_load((root / name / "config.yaml").read_text(encoding="utf-8"))
        context = {key: None for key in config["user_context_items"] + config["system_context_items"]}
        context.update(date_time="2026-09-19 10:00 AM America/Los_Angeles", day_of_week="Saturday", **overrides)
        artifact.append("## " + ("Architect" if name == "work_architect" else "Steward"))
        for kind in ("system", "user"):
            rendered = _jinja_env.get_template(f"dayflow_orchestrator/{name}/prompts/{kind}.j2").render(**context)
            if kind == "user":
                for marker in ("[closed] audit", "[proposed] repair", "[done] verify", "[actionable] docs",
                               "[dispatched] compat", "[waiting] review", "[failed] permission",
                               "[abandoned] old_plan", "[superseded] old_docs", "first change still allowed two workers"):
                    assert marker in rendered
                assert "RAW_INTERNAL_EVIDENCE" not in rendered
                assert "INTERNAL_PROVENANCE_ONLY" not in rendered
            artifact.extend(["### " + kind.capitalize() + " prompt", "```text\n" + rendered + "\n```"])
    target = tmp_path / "dayflow_complex_work_prompts.md"
    if os.environ.get("EMI_WRITE_PROMPT_EXAMPLE") == "1":
        target = Path(__file__).resolve().parents[4] / "docs/design/examples/dayflow_complex_work_prompts.md"
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n\n".join(artifact) + "\n", encoding="utf-8")


def test_worker_refresh_failure_stops_decision_and_clears_old_context(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from app.assistant.agent_classes.WorkPlanner import WorkPlanner
    from app.assistant.tests.dayflow.conftest import FakeBlackboard
    from work_objects import runtime
    def broken_load(wid):
        raise OSError("synthetic store read failure")
    monkeypatch.setattr(runtime, "get_work_context", lambda: SimpleNamespace(
        store=SimpleNamespace(load=broken_load), work_id="work", node_id="task"))
    planner = SimpleNamespace(name="worker", blackboard=FakeBlackboard({"work_projection": "STALE DECISION INPUT"}))
    with pytest.raises(OSError, match="synthetic store read failure"):
        WorkPlanner._refresh_work_projection(planner)
    assert not planner.blackboard.get_state_value("work_projection")
