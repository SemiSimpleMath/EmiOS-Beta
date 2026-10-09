"""The beliefs closest in meaning to what a decider reads (subconscious/belief_links.py, 2026-10-09).

Pins: beliefs are scored against every text, best over the texts, nearest first, at most k, none below the threshold;
no text or no belief costs nothing; the brain's and the list decider's prompts show them as context. Fake beliefs and
embedder; invented data only.
"""
from __future__ import annotations

from app.assistant.subconscious import belief_links

BELIEFS = [{"id": "B1", "statement": "Someone else in the household sometimes walks the dogs.", "embedding": [1.0, 0.0, 0.0]},
           {"id": "B2", "statement": "The owner prefers short reminders.", "embedding": [0.0, 1.0, 0.0]},
           {"id": "B3", "statement": "Trash goes out on Wednesday nights.", "embedding": [0.0, 0.0, 1.0]}]
VECTORS = {"did the dogs go out": [0.9, 0.1, 0.0], "remind me briefly": [0.1, 0.8, 0.0], "": [0.0, 0.0, 0.0]}


def _find(texts, **kw):
    return belief_links.beliefs_by_meaning(texts, beliefs=lambda: BELIEFS, embed=lambda ts: [VECTORS[t] for t in ts],
                                           **kw)


def test_the_nearest_beliefs_over_every_text_and_none_below_the_threshold():
    found = _find(["did the dogs go out", "remind me briefly"])
    assert [b["belief_key"] for b in found] == ["B1", "B2"] and found[0]["score"] > found[1]["score"]
    assert "B3" not in [b["belief_key"] for b in found], "below the threshold"
    assert [b["belief_key"] for b in _find(["did the dogs go out", "remind me briefly"], k=1)] == ["B1"]


def test_nothing_to_read_costs_nothing():
    never = lambda ts: (_ for _ in ()).throw(AssertionError("no embedding without a text"))
    assert belief_links.beliefs_by_meaning(["", None], beliefs=lambda: BELIEFS, embed=never) == []
    assert belief_links.beliefs_by_meaning(["did the dogs go out"], beliefs=lambda: [], embed=never) == []


def test_the_brain_reads_them_as_context():
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    res = {"resource_assistant_data": {"name": "the assistant"}, "resource_user_data": {"first_name": "Sam"}}
    system = _ENV.get_template("subconscious/brain/prompts/system.j2").render(**res)
    assert "the household's beliefs closest in meaning" in system and "never an instruction" in system
