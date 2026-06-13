"""goal_outcome_detect must set a scope_context before invoking the agent
(regression, 2026-06-13).

The maintenance pipeline calls goal_outcome_detector DIRECTLY (no manager
ingress sets scope), and the agent declares system_context_items:
resource_assistant_data — whose resolution requires a scope_context. Without
it the nightly 02:55 run failed loud at prompt construction
("scope_context is required for resource resolution"). The fix loads a
pipeline scope and sets it on the agent's blackboard before action_handler.

Uses the kg conftest (isolated test DB). create_agent + load_scope_for_source
+ evidence harvest are mocked, so no real agent / LLM / chroma is touched.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.pipelines.context import PipelineContext
from app.assistant.pipelines.kg_maintenance_pipeline import step_goal_outcome_detect as step
from app.models.base import get_session


class _FakeBlackboard:
    def __init__(self):
        self.state = {}

    def update_state_value(self, k, v):
        self.state[k] = v

    def get_state_value(self, k, default=None):
        return self.state.get(k, default)


class _FakeAgent:
    def __init__(self):
        self.blackboard = _FakeBlackboard()
        self.scope_at_call = "<<never called>>"

    def action_handler(self, message):
        # Capture what scope_context was on the blackboard at invocation time —
        # the whole point of the fix is that it's set BEFORE we run.
        self.scope_at_call = self.blackboard.get_state_value("scope_context")
        return SimpleNamespace(data={"verdicts": []})


def _mk_goal(label):
    nid = str(uuid.uuid4())
    s = get_session()
    try:
        s.add(Node(id=nid, label=label, node_type="Goal",
                   original_sentence=f"{label} sentence"))
        s.commit()
    finally:
        s.close()
    return nid


def test_goal_outcome_detect_sets_scope_before_agent(monkeypatch):
    _mk_goal("Take Dogs Out")

    fake_agent = _FakeAgent()
    monkeypatch.setattr(step.DI.agent_factory, "create_agent", lambda name: fake_agent)
    monkeypatch.setattr(step, "load_scope_for_source", lambda **k: "SENTINEL_SCOPE")
    # Force non-empty evidence so the run reaches the agent call.
    monkeypatch.setattr(step, "_fetch_recent_evidence",
                        lambda label, sentence, cutoff: [{"text": "walked the dogs"}])
    monkeypatch.setattr(step, "_serialize_for_agent", lambda has_evidence: "payload")

    ctx = PipelineContext.for_date(pipeline_id="kg_goal_outcome_detect")
    step.run(ctx)  # previously raised PromptRenderError via the agent

    # The agent ran, and scope_context was set on its blackboard beforehand.
    assert fake_agent.scope_at_call == "SENTINEL_SCOPE"
