"""The one line an ended work object is known by (subconscious/work_line.py, 2026-10-11).

Pins: the agent reads the work's full objective, every main task with the finalizer's account (a task never run is
shown as not run) and how and when it ended; work not ended is refused; an empty answer gets one correction, then
raises. The brain's pass writes the line of each recently ended work once, records a failure and does not try it
again; readers get the lines written. Scratch database; fake agent; invented data only.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.assistant.subconscious import work_line as W
from belief_engine.intake.store import sqlite_file

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)


def _node(nid, title, status, *, outcome=None, at=None, terminal=None, content="", unit=True, minute=0):
    payload = {}
    if outcome:
        payload["finalizer"] = {"outcome": outcome, "at": at}
    if terminal:
        payload["terminal"] = terminal
    return SimpleNamespace(id=nid, title=title, status=status, payload=payload, content=content, unit=unit,
                           created_at=NOW + timedelta(minutes=minute))


def _work(wid="work_ac", ended=True):
    goal = _node("goal", "Set the AC", "done", unit=False, content="The AC is at COOL 75 for the morning.",
                 terminal={"status": "done", "at": NOW.isoformat(), "reason": "every step finished"} if ended else None)
    tasks = [_node("set", "Set the AC", "closed", outcome="The Nest confirmed COOL 75.", at=NOW.isoformat(), minute=1),
             _node("tell", "Tell the owner", "abandoned", minute=2),
             _node("finding", "", "assumed", unit=False, minute=3)]
    nodes = {n.id: n for n in [goal, *tasks]}
    return SimpleNamespace(id=wid, goal_node_id="goal", nodes=nodes, is_work_unit=lambda n: n.unit)


def test_the_agent_reads_the_whole_report_of_an_ended_work():
    p = W.build_payload(_work())
    assert p["purpose"] == "The AC is at COOL 75 for the morning."
    assert p["tasks"] == [{"title": "Set the AC", "status": "closed", "outcome": "The Nest confirmed COOL 75.",
                           "at": NOW.isoformat()},
                          {"title": "Tell the owner", "status": "abandoned", "outcome": None, "at": None}]
    assert p["ended"] == {"status": "done", "at": NOW.isoformat(), "reason": "every step finished"}
    with pytest.raises(ValueError, match="has not ended"):
        W.build_payload(_work(ended=False))
    from app.assistant.dayflow_orchestrator.work_context import _ENV
    user = _ENV.get_template("subconscious/work_line/prompts/user.j2").render(agent_input=p)
    assert '- "Tell the owner": not run; abandoned when the work ended' in user
    assert "every step finished" in user and NOW.isoformat() not in user


def test_an_empty_answer_gets_one_correction_then_raises():
    seen = []
    assert W.write_line(_work(), call=lambda p: seen.append(p) or ({"line": ""} if len(seen) == 1 else
                                                                     {"line": " AC set  at 6. "})) == "AC set at 6."
    assert seen[1]["correction"] == "the answer had no line"
    with pytest.raises(ValueError, match="still empty"):
        W.write_line(_work(), call=lambda p: {"line": " "})


def test_the_pass_writes_each_line_once_and_a_failure_is_not_tried_again(tmp_path):
    conn = sqlite_file(tmp_path / "w.db")
    ended = lambda now: [_work("work_ac"), _work("work_bad")]
    calls = []

    def call(p):
        calls.append(p)
        if len(calls) > 1:
            raise RuntimeError("model unavailable")
        return {"line": "The AC was set to COOL 75 at 6:00 AM."}
    assert W.run_lines(now_utc=NOW, call=call, connect=conn, ended=ended) == {"work_lines_written": 1,
                                                                             "work_lines_failed": 1}
    assert W.lines(["work_ac", "work_bad", "work_other"], connect=conn) == {
        "work_ac": "The AC was set to COOL 75 at 6:00 AM."}
    assert W.run_lines(now_utc=NOW, call=lambda p: pytest.fail("each work once"), connect=conn, ended=ended) == {
        "work_lines_written": 0, "work_lines_failed": 0}
    with conn(False) as c:
        assert c.execute("SELECT error FROM brain_work_lines WHERE work_id='work_bad'").fetchone()[0] == \
            "RuntimeError: model unavailable"
