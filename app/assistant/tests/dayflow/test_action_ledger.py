"""The work object records what it DID, and the planner's projection shows it.

2026-09-12: a stuck flea-medication goal sent eleven near-identical emails to one recipient
in fifty-two minutes and raised seven tickets, six of which expired. The architect's rendered
prompt for that goal — 52,836 characters — contained the string "email" zero times. Every
guard in the system counted timeouts, prunes or elapsed minutes. None counted sends, because
nothing recorded them.

Two properties are pinned here:
  1. The ledger is keyed on the WORK OBJECT, so it survives the replanning that resets every
     node-keyed counter. That reset is how the loop escaped the three-timeout ask ceiling.
  2. The projection the planner reads shows the acts, their outcomes, and calls out repeats.

Also pinned: the goal line renders from `content`, not the 80-char `title` slice that ate the
deadline off the real goal.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from app.assistant.dayflow_orchestrator.work_portfolio import render_work_portfolio
from work_objects.store import WorkStore

FULL_GOAL = (
    "Resolve Bonnie and Clyde's flea-medication details and ensure an appropriate preventive "
    "is arranged for their September 25, 2026 appointments, with the household decision or "
    "required information obtained by September 18, 2026."
)


@pytest.fixture
def store():
    path = os.path.join(tempfile.mkdtemp(), "work.db")
    st = WorkStore(path)
    yield st
    st.close()


def _goal(store, objective=FULL_GOAL):
    # work_persist mints the title as objective[:80] — reproduce that faithfully.
    return store.apply("create_work_object",
                       {"title": objective[:80], "goal_content": objective},
                       actor="steward")


def test_the_goal_line_shows_the_deadline_the_title_slice_cuts_off(store):
    wo = _goal(store)
    # The stored title is the 80-char slice and both dates fall off the end of it.
    assert len(wo.title) == 80
    assert "September 18, 2026" not in wo.title
    assert "September 25, 2026" not in wo.title
    # The projection must still carry them, because they are the whole point of the goal.
    rendered = render_work_portfolio(store.load(wo.id))
    assert "September 18, 2026" in rendered
    assert "September 25, 2026" in rendered


def test_sends_are_recorded_against_the_work_object_not_the_node(store):
    wo = _goal(store)
    # Three different nodes ask the same question — the shape that reset the ask ceiling.
    for i in range(3):
        store.apply("add_node", {"work_id": wo.id, "type": "subtask",
                                 "title": f"Obtain confirmation (attempt {i})",
                                 "parent_id": wo.goal_node_id}, actor="architect")
        store.apply("record_action", {
            "work_id": wo.id, "node_id": f"node_generation_{i}", "channel": "email",
            "target": "ksuttorp@example.com", "summary": f"Confirm flea meds (attempt {i})",
        }, actor="send_email")

    back = store.load(wo.id)
    assert len(back.actions) == 3, "the ledger must not be scoped to a node id"
    assert {a.node_id for a in back.actions} == {
        "node_generation_0", "node_generation_1", "node_generation_2"}


def test_the_projection_shows_what_was_sent_and_flags_the_repeats(store):
    wo = _goal(store)
    for i in range(4):
        store.apply("record_action", {
            "work_id": wo.id, "channel": "email", "target": "ksuttorp@example.com",
            "summary": ["Quick: flea medication and supply",
                        "Reminder: details needed by Sep 20",
                        "Urgent: details needed by Sep 18",
                        "Final request: confirm product"][i],
        }, actor="send_email")
    store.apply("record_action", {
        "work_id": wo.id, "channel": "ticket", "target": "user",
        "summary": "Please confirm Bonnie and Clyde's flea plan", "outcome": "expired",
    }, actor="dispatch_sweeper")

    rendered = render_work_portfolio(store.load(wo.id))

    assert "ACTIONS TAKEN (5)" in rendered
    assert "ksuttorp@example.com" in rendered
    assert "Final request: confirm product" in rendered
    # An expired ask must still be visible; it is the one you must not silently repeat.
    assert "[expired]" in rendered
    # And the burst is called out rather than reading as five ordinary rows.
    assert "4 separate email messages to ksuttorp@example.com" in rendered


def test_a_goal_with_no_actions_says_nothing_about_actions(store):
    wo = _goal(store)
    assert "ACTIONS TAKEN" not in render_work_portfolio(store.load(wo.id))


def test_record_action_refuses_a_row_with_no_channel(store):
    wo = _goal(store)
    with pytest.raises(ValueError, match="channel"):
        store.apply("record_action", {"work_id": wo.id, "target": "x"}, actor="test")


def test_the_ledger_can_be_read_back_for_one_recipient(store, monkeypatch):
    from app.assistant.dayflow_orchestrator import action_ledger

    wo = _goal(store)
    for target in ("ksuttorp@example.com", "ksuttorp@example.com", "vet@example.com"):
        store.apply("record_action", {"work_id": wo.id, "channel": "email",
                                      "target": target, "summary": "confirm"}, actor="send_email")
    monkeypatch.setattr(action_ledger, "get_dayflow_work_store", lambda: store, raising=False)
    monkeypatch.setitem(
        __import__("sys").modules,
        "app.assistant.dayflow_orchestrator.work_store",
        type("M", (), {"get_dayflow_work_store": staticmethod(lambda: store)}),
    )
    hits = action_ledger.recent_outbound(wo.id, channel="email", target="ksuttorp@example.com")
    assert len(hits) == 2


def test_the_thread_name_carries_the_work_context():
    import threading

    from app.assistant.dayflow_orchestrator.action_ledger import current_work_context
    from app.assistant.dayflow_orchestrator.work_session import session_id_for

    captured = {}

    def run():
        captured["ctx"] = current_work_context()

    t = threading.Thread(target=run, name=session_id_for("work_abc", "node_xyz"))
    t.start()
    t.join()
    assert captured["ctx"] == ("work_abc", "node_xyz")
    # Off a work session there is nothing to charge the act to.
    assert current_work_context() == (None, None)
