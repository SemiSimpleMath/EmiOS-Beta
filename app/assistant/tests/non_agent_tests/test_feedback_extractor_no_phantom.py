"""Guard: feedback-extractor output lands in the belief catalog without fabricating phantoms.

When the user says "kids don't like zucchini," the extractor emits a 'confirms' on the dislike
AND a 'contradicts' on a mirror "kids WILL eat zucchini" belief. Minting the mirror would create
an affirmative belief carrying only negative evidence (3 such phantoms reached production in
2026-06). Since the 2026-09-29 cutover the writer is the intake store: a pushback attaches as
contradicting evidence only when dedup finds the belief held ('same'); otherwise it is skipped.

Real IntakeStore in a scratch file; the dedup model, pods and embedder are fakes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.assistant.subconscious.feedback_extractor_persist as fep
from belief_engine.intake.store import IntakeStore, sqlite_file


class _Pods:
    def __init__(self, *ids):
        self.pods = {i: SimpleNamespace(pod_id=i, tags=["unprocessed"], metadata={
            "text": f"comment {i}", "submitted_at_utc": "2026-09-28T18:30:00+00:00"}) for i in ids}
        self.put_ids = []

    def get(self, pod_id):
        return self.pods.get(pod_id)

    def put(self, pod):
        self.put_ids.append(pod.pod_id)


def _embed(texts):
    return [[1.0, float(len(t) % 7)] for t in texts]


@pytest.fixture
def intake(tmp_path):
    s = IntakeStore(sqlite_file(tmp_path / "s.db"))
    s.apply("2026-09-01", {"statement": "The household likes roasted zucchini.", "kind": "stable_preference",
                           "scope": "chronic", "sources": [{"time": "2026-09-01 12:00", "kind": "said",
                           "relation": "support", "text": "zucchini was great", "source_ref": "message:1"}]},
            [1.0, 0.0], {"verdict": "new"})
    return s


def _run(intake, pods, verdicts, extractions):
    calls = iter(verdicts)
    import belief_engine.intake.agents as A
    orig = A.dedup
    A.dedup = lambda atom, day, candidates, scope_ctx: next(calls)
    try:
        return fep.apply_feedback_extractor_output(
            {"extractions": extractions, "skipped": []},
            intake_store=intake, pod_store=pods, embed_texts=_embed, scope_ctx=None)
    finally:
        A.dedup = orig


def test_confirms_becomes_a_belief_sourced_to_the_comment(intake):
    pods = _Pods("c1")
    out = _run(intake, pods, [{"verdict": "new", "target": None}], [
        {"signal_type": "confirms", "statement": "The kids dislike soup.", "scope": "chronic",
         "source_comment_pod_id": "c1"}])
    new = next(b for b in intake.beliefs() if b["id"] == "B2")
    assert new["statement"] == "The kids dislike soup." and new["kind"] == "stable_preference"
    assert new["sources"][0]["text"] == "comment c1" and new["sources"][0]["kind"] == "said"
    assert intake.has_evidence("B2", "pod:c1")
    assert pods.pods["c1"].metadata["extracted_belief_ids"] == ["B2"]
    assert "processed" in pods.pods["c1"].tags and out["upserted_count"] == 1


def test_pushback_on_a_belief_nobody_holds_is_never_minted(intake):
    pods = _Pods("c1")
    out = _run(intake, pods, [{"verdict": "new", "target": None}], [
        {"signal_type": "contradicts", "statement": "The kids will eat zucchini.", "scope": "chronic",
         "source_comment_pod_id": "c1"}])
    assert [b["id"] for b in intake.beliefs()] == ["B1"]
    assert out["phantom_skipped_count"] == 1
    assert "processed" in pods.pods["c1"].tags          # read and judged; nothing to retry


def test_pushback_on_a_held_belief_attaches_as_contradicting_evidence(intake):
    pods = _Pods("c1")
    _run(intake, pods, [{"verdict": "same", "target": "B1", "sources": [1], "reasoning": "same claim"}], [
        {"signal_type": "rejects", "statement": "The household likes roasted zucchini.", "scope": "chronic",
         "source_comment_pod_id": "c1"}])
    held = intake.beliefs()[0]
    assert [s["relation"] for s in held["sources"]] == ["support", "contradict"]
    assert held["statement"] == "The household likes roasted zucchini."


def test_a_failed_extraction_leaves_its_comment_queued(intake):
    pods = _Pods("c1")
    out = _run(intake, pods, [], [
        {"signal_type": "confirms", "statement": "x", "scope": "chronic", "source_comment_pod_id": "gone"},
        {"signal_type": "confirms", "statement": "", "scope": "chronic", "source_comment_pod_id": "c1"}])
    assert out["upsert_failed_count"] == 2 and out["comments_marked_processed"] == 0
    assert pods.pods["c1"].tags == ["unprocessed"]
