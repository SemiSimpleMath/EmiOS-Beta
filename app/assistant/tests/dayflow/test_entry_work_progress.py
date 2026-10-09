"""A decider sees what an entry's work is still waiting for (2026-10-09).

Pins: the work view of an active work object carries its progress, and an entry's work as the deciders read it
(shared/brain/entry_work.j2, through concern_entries.j2) names each step to come with what it waits for, so an email
the work is waiting for is not read as new; ended work shows how it ended, without steps to come. Isolated dayflow
store (conftest); invented data only.
"""
from __future__ import annotations

from app.assistant.dayflow_orchestrator.work_context import _ENV
from app.assistant.subconscious import work_links


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _waiting_work(store):
    wo = store.apply("create_work_object", {"title": "The quote is in hand by Friday",
                                            "goal_content": "The quote is in hand by Friday",
                                            "satisfied_when_kind": "all_owned_children_done"})
    node = f"{wo.id}_tell"
    store.apply("add_node", {"work_id": wo.id, "id": node, "type": "subtask", "parent_id": wo.goal_node_id,
                             "title": "Tell the owner the quote"})
    store.apply("defer_node", {"work_id": wo.id, "node_id": node, "wake_kind": "event", "wake_at": None,
                               "wake_ref": "the contractor's reply with the quote arrives"})
    return wo.id


def _render(work):
    entry = {"id": "con:ab12", "what": "The quote is in hand by Friday", "assistant_part": "tell the owner",
             "when_local": "now", "status": "open", "updated_at": "2026-10-09T14:00:00+00:00", "work": work}
    return _ENV.get_template("shared/brain/concern_entries.j2").render(entries=[entry], cancelled=[])


def test_an_entrys_active_work_shows_what_its_steps_wait_for():
    work_id = _waiting_work(_store())
    view = work_links.work_view(work_id, link="made from this entry")
    assert view["progress"]["to_come"][0]["wait"] == {"kind": "watching",
                                                      "condition": "the contractor's reply with the quote arrives"}
    text = _render(view)
    assert "Tell the owner the quote: waiting until: the contractor's reply with the quote arrives" in text


def test_ended_work_shows_its_ending_without_steps_to_come():
    work_id = _waiting_work(_store())
    view = {**work_links.work_view(work_id, link="made from this entry"), "status": "done",
            "ended_because": "done: the owner has the quote"}
    text = _render(view)
    assert "ended because: done: the owner has the quote" in text and "waiting until" not in text
