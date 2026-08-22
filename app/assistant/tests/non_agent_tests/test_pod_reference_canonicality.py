"""pod_id canonical or invisible — enforced at the final-answer envelope (2026-08-22).

The final-answer agent TYPES pod_references, and typed ids arrive as slugified
titles ("msa-september-30-2026") that can never be fetched, attached, or linked —
the MSA delivery shipped without its report-page link because both attached refs
were fabrications while the two real research_finding pods sat unreferenced.
attach_carry_through now (1) merges the run's research_notebook — the canonical
ids recorded at mint time — as a ground-truth source, and (2) drops any ref that
is not a well-formed pod URI.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.agent_runtime.services.final_answer_normalizer import FinalAnswerNormalizer


class _FakeBlackboard:
    def __init__(self, state):
        self._s = state

    def get_state_value(self, key, default=None):
        return self._s.get(key, default)


def test_slug_refs_dropped_and_notebook_ids_survive():
    bb = _FakeBlackboard({
        "research_notebook": [
            {"unit": "msa terms", "one_liner": "MSA analysis",
             "pod_id": "datapod:research_finding:19897ee64b25"},
        ],
    })
    payload = {"final_answer_answer": "done",
               "pod_references": [{"pod_id": "msa-september-30-2026", "one_liner": "typed slug"}]}
    out = FinalAnswerNormalizer.attach_carry_through(payload, bb)
    ids = [p["pod_id"] for p in out["pod_references"]]
    assert ids == ["datapod:research_finding:19897ee64b25"]


def test_all_invalid_refs_become_invisible_not_kept():
    bb = _FakeBlackboard({})
    payload = {"pod_references": [{"pod_id": "some-invented-slug", "one_liner": "x"}]}
    out = FinalAnswerNormalizer.attach_carry_through(payload, bb)
    assert out["pod_references"] == []


def test_canonical_llm_refs_pass_through():
    bb = _FakeBlackboard({})
    payload = {"pod_references": [
        {"pod_id": "datapod:research_finding:aaa111bbb222", "one_liner": "real"}]}
    out = FinalAnswerNormalizer.attach_carry_through(payload, bb)
    assert [p["pod_id"] for p in out["pod_references"]] == ["datapod:research_finding:aaa111bbb222"]
