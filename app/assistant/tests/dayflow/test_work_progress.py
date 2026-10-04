"""Work says where it stands, in words a reader outside dayflow understands (2026-10-04).

Pins: every main-task status has a place (done, dropped, or still to come with what it waits for) and an
unknown one raises; each wait is worded (ready, scheduled, after another task, watching for an event, being
done, waiting for the owner's reply to the question the ticket asked, being checked, failed); steps are
listed in the order the plan runs them; a pending re-plan is said; an ended work says how it ended;
a judged receipt carries the progress at that moment and the brain event words it.
docs/design/work_progress_for_outside_readers_2026-10-04.md. Invented data only.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.assistant.dayflow_orchestrator.work_context import render_view
from work_objects.model import ActionRecord, WorkNode, WorkObject
from work_objects.progress import work_progress
from work_objects.store import TRANSITIONS, WorkStore

NOW = datetime(2026, 3, 10, 16, 0, tzinfo=timezone.utc)
T0 = datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc)


def _work(*tasks, status="active", edges=(), actions=(), goal_payload=None):
    wo = WorkObject(id="work_x", title="Bake sale", status=status)
    goal = wo.add_node(WorkNode(work_id=wo.id, id="goal", type="goal", title="Bake sale",
                                payload=goal_payload or {}))
    wo.goal_node_id = goal.id
    for t in tasks:
        wo.add_node(WorkNode(work_id=wo.id, type="subtask", parent_id="goal", created_at=T0, **t))
    for src, dst in edges:
        wo.add_edge(src, dst, "depends_on")
    wo.actions.extend(actions)
    return wo


def _render(p):
    return render_view("work_progress", p=p, pad="")


def _wait_line(p, title):
    return next(line for line in _render(p).splitlines() if line.startswith(f"  - {title}:"))


@pytest.mark.parametrize("task,phrase", [
    ({"status": "actionable"}, "ready now"),
    ({"status": "proposed"}, "ready now"),
    ({"status": "waiting", "wake_kind": "time", "wake_at": NOW + timedelta(hours=2)}, "scheduled for "),
    ({"status": "proposed", "wake_kind": "event", "wake_ref": "the owner has gone to bed"},
     "waiting until: the owner has gone to bed"),
    ({"status": "dispatched"}, "being done now"),
    ({"status": "done"}, "done, its result is being checked"),
    ({"status": "failed"}, "did not succeed; the assistant is deciding the next attempt"),
])
def test_each_open_step_says_what_it_waits_for(task, phrase):
    p = work_progress(_work({"id": "t", "title": "Bake", **task}), NOW)
    assert p["state"] == "in_progress" and [t["title"] for t in p["to_come"]] == ["Bake"]
    assert phrase in _wait_line(p, "Bake")


def test_an_open_ask_names_the_question_and_when_it_was_asked():
    asked = ActionRecord(work_id="work_x", node_id="ask", ts=T0, channel="ticket", target="ui",
                         summary="Who bakes on Sunday?")
    p = work_progress(_work({"id": "ask", "title": "Ask who bakes", "status": "dispatched", "wake_kind": "user_reply",
                             "wake_ref": "ticket_1"}, actions=[asked]), NOW)
    assert p["to_come"][0]["wait"] == {"kind": "owner_reply", "question": "Who bakes on Sunday?",
                                       "asked_at": T0.isoformat()}
    assert "waiting for the owner's reply to \"Who bakes on Sunday?\" (asked " in _wait_line(p, "Ask who bakes")


def test_finished_and_dropped_steps_and_the_plan_order():
    p = work_progress(_work(
        {"id": "deliver", "title": "Deliver the briefing", "status": "proposed"},
        {"id": "compile", "title": "Compile the briefing", "status": "closed",
         "payload": {"finalizer": {"verdict": "achieved", "at": T0.isoformat()}}},
        {"id": "old", "title": "Print it", "status": "abandoned", "payload": {"terminal": {"reason": "not needed"}}},
        edges=[("compile", "deliver")]), NOW)
    assert p["done"] == [{"title": "Compile the briefing", "verdict": "achieved", "at": T0.isoformat()}]
    assert p["dropped"] == [{"title": "Print it", "reason": "not needed"}]
    assert p["to_come"] == [{"title": "Deliver the briefing", "wait": {"kind": "ready"}}]
    text = _render(p)
    assert text.startswith("In progress. The assistant carries this work until every step is done, "
                           "or abandons it with a reason.")
    assert "Done:\n  - Compile the briefing (achieved " in text and ("Still to come in this work (the assistant does each step itself, as part of this work):\n"
                                                                     "  - Deliver the briefing: ready now; the assistant does it next") in text


def test_a_step_behind_an_unfinished_one_says_after_it_and_comes_after_it():
    p = work_progress(_work({"id": "deliver", "title": "Deliver", "status": "proposed"},
                            {"id": "compile", "title": "Compile", "status": "dispatched"},
                            edges=[("compile", "deliver")]), NOW)
    assert [t["title"] for t in p["to_come"]] == ["Compile", "Deliver"]
    assert _wait_line(p, "Deliver").endswith('after "Compile"')


def test_a_pending_replan_is_said():
    p = work_progress(_work({"id": "t", "title": "Bake", "status": "failed",
                             "payload": {"finalizer": {"verdict": "retry", "next_step": "new_approach"}}}), NOW)
    assert p["replanning"] and "the assistant is re-planning this work" in _render(p)


def test_an_ended_work_says_how_it_ended():
    done = work_progress(_work({"id": "t", "title": "Bake", "status": "closed"}, status="done",
                               goal_payload={"terminal": {"at": T0.isoformat(), "reason": "every step finished"}}), NOW)
    assert (done["state"], done["ended_at"], done["to_come"]) == ("done", T0.isoformat(), [])
    assert _render(done).startswith("Ended ") and "done, every step finished." in _render(done)
    gone = work_progress(_work({"id": "t", "title": "Bake", "status": "abandoned"}, status="abandoned",
                               goal_payload={"terminal": {"at": T0.isoformat(), "reason": "architect: the sale was cancelled"}}), NOW)
    assert _render(gone).startswith("Abandoned ") and ": architect: the sale was cancelled" in _render(gone)


def test_every_main_task_status_has_a_place_and_an_unknown_one_raises():
    for work_status in ("active", "done"):
        for status in TRANSITIONS["spine"]:
            p = work_progress(_work({"id": "t", "title": "Bake", "status": status}, status=work_status), NOW)
            placed = [t["title"] for key in ("done", "dropped", "to_come", "unfinished") for t in p[key]]
            assert placed == ["Bake"], (work_status, status)
            _render(p)
    with pytest.raises(ValueError, match="no wording"):
        work_progress(_work({"id": "t", "title": "Bake", "status": "pondering"}), NOW)


def test_a_judged_receipt_carries_the_progress_and_the_event_words_it(tmp_path):
    from app.assistant.subconscious.concern_feedback import _event_text
    store = WorkStore(str(tmp_path / "work.db"))
    try:
        wo = store.apply("create_work_object", {"title": "Morning briefing",
                                                "constraints": {"objective": "Deliver the morning briefing"}})
        for nid, title, status in (("compile", "Compile the briefing", "dispatched"),
                                   ("deliver", "Deliver the briefing", "proposed")):
            store.apply("add_node", {"work_id": wo.id, "id": nid, "type": "subtask", "title": title,
                                     "parent_id": wo.goal_node_id, "status": status})
        store.apply("add_edge", {"work_id": wo.id, "src": "compile", "dst": "deliver", "relation": "depends_on"})
        store.apply("record_result", {"work_id": wo.id, "node_id": "compile", "expected_dispatch_epoch": 0,
                                      "evidence_id": "draft", "title": "Draft", "answer": "drafted", "status": "done"})
        store.apply("finalize_task", {"work_id": wo.id, "node_id": "compile", "expected_dispatch_epoch": 0,
                                      "finalizer": {"verdict": "achieved", "next_step": "",
                                                    "outcome": "The draft is complete and remains unsent."}},
                    actor="finalizer")
        [judged] = [r for r in store.pending_concern_feedback() if r["outcome"] == "judged"]
        assert [t["title"] for t in judged["payload"]["progress"]["to_come"]] == ["Deliver the briefing"]
        text = _event_text(judged)
        assert "What happened in this task: The draft is complete and remains unsent." in text
        assert ("Where the work stands now:\n  In progress. The assistant carries this work until every step is "
                "done, or abandons it with a reason.") in text
        assert ("  Still to come in this work (the assistant does each step itself, as part of this work):\n"
                "    - Deliver the briefing: ready now; the assistant does it next") in text
    finally:
        store.close()


def test_a_step_the_works_ending_closed_is_unfinished_and_the_reason_is_said_once():
    p = work_progress(_work({"id": "t", "title": "Bake", "status": "abandoned",
                             "payload": {"terminal": {"verdict": "cascade", "reason": "work_object_abandoned: cancelled"}}},
                            status="abandoned", goal_payload={"terminal": {"at": T0.isoformat(), "reason": "cancelled"}}), NOW)
    assert (p["dropped"], p["unfinished"]) == ([], [{"title": "Bake"}])
    assert _render(p).count("cancelled") == 1


def test_a_step_left_open_when_the_work_ended_is_not_still_to_come():
    p = work_progress(_work({"id": "t", "title": "Bake", "status": "done"}, status="done",
                            goal_payload={"terminal": {"at": T0.isoformat(), "reason": "r"}}), NOW)
    assert (p["to_come"], p["unfinished"]) == ([], [{"title": "Bake"}])
    assert "Not finished when the work ended:\n  - Bake" in _render(p) and "Still to come" not in _render(p)
