"""KG ingestion stops at its first failed call (owner, 2026-10-05: "If any call fails in KG, we should treat that as a
stop processing").

Pins: the window critic raises instead of accepting every line; a failed window stops the cycle, writes no verdict row
and is counted in the failure ledger; the enricher gets one targeted correction for State/Event nodes it left out,
then raises; the runner treats only SQLite lock errors as transient; the segmenter refuses an answer that leaves
messages out of every window; the resolver refuses a message with no resolved text.

Uses the kg conftest (isolated test DB). Fake agents; invented data only.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from app.assistant.kg import failure_ledger
from app.assistant.pipelines.kg_pipeline import runner
from app.assistant.pipelines.kg_pipeline.steps import critique_and_extract as CE
from app.assistant.pipelines.kg_pipeline.steps import enrich_extraction as EN
from app.assistant.pipelines.kg_pipeline.steps import resolve_messages as RM
from app.assistant.pipelines.kg_pipeline.steps import segment_messages as SM
from app.models.base import get_session


class _Agent:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.config = {}
        self.calls = []

    def action_handler(self, msg):
        self.calls.append(msg.agent_input)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(data=a)


@pytest.fixture
def agent(monkeypatch):
    from app.assistant.ServiceLocator.service_locator import DI
    holder = {}
    monkeypatch.setattr(DI.agent_factory, "create_agent", lambda name: holder.get("agent"))
    monkeypatch.setattr(CE, "load_scope_for_source", lambda **k: None)
    monkeypatch.setattr(EN, "load_scope_for_source", lambda **k: None)
    return holder


@pytest.fixture(autouse=True)
def _ledger():
    s = get_session()
    try:
        failure_ledger._ensure(s)
        s.query(failure_ledger.KGItemFailure).delete()
        s.commit()
    finally:
        s.close()


def test_the_window_critic_raises_instead_of_accepting_every_line(agent):
    step = CE.CritiqueAndExtractStep()
    agent["agent"] = _Agent(RuntimeError("Connection error."))
    with pytest.raises(RuntimeError, match="Connection error"):
        step._call_window_critic(["Sam: I moved to Mondays."], [])
    agent["agent"] = _Agent("not a dict")
    with pytest.raises(RuntimeError, match="no answer"):
        step._call_window_critic(["Sam: I moved to Mondays."], [])
    agent["agent"] = _Agent({"extractable_lines": [{"user_message_index": 7, "reason": "x"}], "overall_reason": "x"})
    with pytest.raises(ValueError, match="named no user line"):
        step._call_window_critic(["Sam: I moved to Mondays."], [])
    agent["agent"] = _Agent({"extractable_lines": [], "overall_reason": "nothing"})
    assert step._call_window_critic(["Sam: ok"], []) == {"extractable": False, "kept_indices": [],
                                                          "reason": "nothing"}
    agent["agent"] = None
    with pytest.raises(RuntimeError, match="Failed to create agent"):
        step._call_window_critic(["Sam: ok"], [])


def test_a_failed_window_stops_the_cycle_and_is_counted(monkeypatch):
    step = CE.CritiqueAndExtractStep()
    monkeypatch.setattr(step, "_claim_pending_window_ids", lambda limit: ["w1", "w2"])
    seen = []

    def process(window_id):
        seen.append(window_id)
        raise RuntimeError("Connection error.")
    monkeypatch.setattr(step, "_process_window", process)
    with pytest.raises(RuntimeError):
        step.run_one_cycle(None)
    assert seen == ["w1"]
    s = get_session()
    try:
        assert s.get(failure_ledger.KGItemFailure, ("kg_window", "w1")).failures == 1
    finally:
        s.close()


def test_the_enricher_gets_one_correction_for_nodes_it_left_out(agent):
    step = EN.EnrichExtractionStep()
    nodes = [{"temp_id": "s1", "node_type": "State", "label": "Call", "sentence": "A call on Mondays."},
             {"temp_id": "s2", "node_type": "Event", "label": "Trip", "sentence": "A trip in June."}]
    first = {"Nodes": [{"temp_id": "s1", "start_date": "2026-03-03"}]}
    agent["agent"] = _Agent(first, {"Nodes": [{"temp_id": "s2", "start_date": "2026-06-01"}]})
    out = step._call_meta_data_add(nodes, [], resolved_window="w", message_timestamp="t")
    assert [n.get("start_date") for n in out] == ["2026-03-03", "2026-06-01"]
    assert [n["temp_id"] for n in __import__("json").loads(agent["agent"].calls[1]["nodes"])] == ["s2"], \
        "the correction asks for exactly the node left out"
    agent["agent"] = _Agent(first, {"Nodes": []})
    with pytest.raises(ValueError, match="left out"):
        step._call_meta_data_add(nodes, [], resolved_window="w", message_timestamp="t")


def test_only_sqlite_lock_errors_are_transient():
    assert runner._is_database_locked(sqlite3.OperationalError("database is locked"))
    assert not runner._is_database_locked(sqlite3.OperationalError("no such table: kg_window"))


def test_the_segmenter_must_place_every_message(monkeypatch):
    step = SM.SegmentMessagesStep()
    from datetime import datetime, timezone
    msgs = [{"id": f"m{i}", "role": "user", "timestamp": datetime(2026, 10, 5, 12, i, tzinfo=timezone.utc),
             "resolved_text": "x"} for i in range(3)]
    monkeypatch.setattr(step, "_find_next_day_ready_to_segment", lambda: datetime(2026, 10, 5).date())
    monkeypatch.setattr(step, "_load_day_to_segment", lambda day: {"messages": msgs, "unresolved_count": 0})
    bounds = {"start_message_id": "m0", "end_message_id": "m1", "should_process": True, "reasoning": "r"}
    monkeypatch.setattr(step, "_call_segmenter", lambda m: {"message_bounds": [
        {"message_id": "m0", "bounds": bounds}, {"message_id": "m1", "bounds": bounds}]})
    monkeypatch.setattr(step, "_persist_windows", lambda *a: pytest.fail("nothing is written"))
    with pytest.raises(ValueError, match=r"left 1 of 3 messages"):
        step.run_one_cycle(None)


def test_the_resolver_refuses_a_message_with_no_resolved_text():
    step = RM.ResolveMessagesStep()
    m = {"unified_log_id": "u-1", "timestamp": None, "raw_text": "hi"}
    with pytest.raises(ValueError, match="no text"):
        step._write_batch([m], {"u_1": {"resolved_text": "  "}})
