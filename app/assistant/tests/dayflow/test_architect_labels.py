"""The architect as a projection (docs/design/objects_and_projections_2026-10-08.md, module 3).

Pins: the architect sees its tasks as T, other work as W, never an id; its answer names existing tasks by label in
depends_on, abandon_node_ids and duplicate_of, and new tasks by the node_id it chose; what a task must read is
`uses`, what already did the goal is `covered_by`; code maps them to ids and keeps uses, covered_by and duplicate_of
as relations; a label written into words, an unknown label or one of the wrong kind is refused with every problem
named. The worker is shown what its task uses, with the ids its tools take; a reader of an ended goal sees what
covered it, dated. Isolated dayflow store (conftest); invented data only.
"""
from __future__ import annotations

import pytest

from app.assistant.control_nodes import work_architect_node as A
from app.assistant.dayflow_orchestrator.work_architect_apply import apply_architect_dag
from app.assistant.subconscious import relations


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _goal(store, objective, *tasks):
    wo = store.apply("create_work_object", {"title": objective, "goal_content": objective,
                                            "satisfied_when_kind": "all_owned_children_done"})
    for i, title in enumerate(tasks):
        store.apply("add_node", {"work_id": wo.id, "id": f"{wo.id}_t{i}", "type": "subtask",
                                 "parent_id": wo.goal_node_id, "title": title})
    return store.load(wo.id)


def test_labels_map_to_ids_and_new_tasks_keep_their_names():
    store = _store()
    wo = _goal(store, "Book the dentist", "Find a free slot", "Old approach")
    other = _goal(store, "Dentist booked last week")
    p, ids = A._projection(wo, [wo, other], [])
    labels = p.labels
    assert labels["G"] == ["work", wo.id] and labels["T1"] == ["task", f"{wo.id}_t0"] and labels["W1"] == ["work", other.id]
    answer = {"architect_summary": "Ask, then book.",
              "nodes": [{"node_id": "ask_day", "title": "Ask which day", "detail": "Ask the user which day suits.",
                         "depends_on": ["T1"], "uses": ["T1"]},
                        {"node_id": "book", "title": "Book it", "detail": "Book the day the user chose.",
                         "depends_on": ["ask_day"], "uses": ["ask_day", "W1"]}],
              "abandon_node_ids": ["T2"], "abandon_reason": "The old approach is replaced by asking first.",
              "duplicate_of": []}
    mapped, links = A._resolve(answer, labels, wo)
    assert mapped["nodes"][0]["depends_on"] == [f"{wo.id}_t0"] and mapped["nodes"][1]["depends_on"] == ["ask_day"]
    assert mapped["abandon_node_ids"] == [f"{wo.id}_t1"]
    assert links["uses"] == {"ask_day": [("task", f"{wo.id}_t0")], "book": [("new", "ask_day"), ("work", other.id)]}


def test_every_problem_is_named_and_nothing_is_applied():
    store = _store()
    wo = _goal(store, "Book the dentist", "Find a free slot")
    other = _goal(store, "Dentist booked last week")
    labels = A._projection(wo, [wo, other], [])[0].labels
    bad = {"architect_summary": "s",
           "nodes": [{"node_id": "T1", "title": "t", "detail": "Read W1 first.", "depends_on": ["T9", "ghost"],
                      "uses": ["G"]}],
           "abandon_node_ids": ["W1"], "end_goal": None}
    with pytest.raises(ValueError) as refused:
        A._resolve(bad, labels, wo)
    text = str(refused.value)
    for problem in ("a new task's node_id is a short name you choose, not a label",
                    "depends_on 'T9' is neither", "depends_on 'ghost' is neither",
                    "uses 'G' is not a task, record, source or work given",
                    "['W1'] written into its words", "abandon_node_ids: 'W1' is not a task of this goal"):
        assert problem in text
    with pytest.raises(ValueError, match="covered_by: 'T1' is not other work given"):
        A._resolve({"end_goal": {"status": "abandoned", "reason": "done elsewhere", "covered_by": ["T1"]}},
                   labels, wo)


def test_uses_covered_by_and_duplicates_become_relations_readers_see():
    """2026-10-08: the architect ended the evening's phone-charge reminder "already achieved in the recently
    completed work '<title>'", the day before's; nothing could check it, and the reminder was lost twice."""
    from app.assistant.dayflow_orchestrator.work_context import task_uses
    from app.assistant.subconscious.work_links import work_view
    store = _store()
    wo = _goal(store, "Remind about the phone tonight", "Check the charger")
    yesterday = _goal(store, "Remind about the phone yesterday")
    p, _ = A._projection(wo, [wo, yesterday], [])
    answer = {"architect_summary": "One reminder.",
              "nodes": [{"node_id": "remind", "title": "Remind", "detail": "Tell the user to charge the phone.",
                         "depends_on": [], "uses": ["T1"]}]}
    mapped, links = A._resolve(answer, p.labels, wo)
    res = apply_architect_dag(store, wo.id, mapped["nodes"], expected_updated_at=wo.updated_at)
    A._keep_links(wo, links, res["added"])
    [added] = res["added"]
    assert relations.targets(("task", added), "uses") == [("task", f"{wo.id}_t0")]
    uses = task_uses(store.load(wo.id), added)
    assert uses == [{"kind": "task", "id": f"{wo.id}_t0", "what": 'the task "Check the charger"',
                     "how": "work_graph_peek, then work_artifact_fetch"}], "the worker gets the id its tool takes"

    ending = {"end_goal": {"status": "abandoned", "reason": "Yesterday's reminder covers it.", "covered_by": ["W1"]}}
    wo = store.load(wo.id)
    mapped, links = A._resolve(ending, p.labels, wo)
    assert mapped["end_goal"] == {"status": "abandoned", "reason": "Yesterday's reminder covers it."}
    apply_architect_dag(store, wo.id, [], end_goal=mapped["end_goal"], expected_updated_at=wo.updated_at)
    A._keep_links(wo, links, [])
    view = work_view(wo.id, link="ended")
    assert [c["work_id"] for c in view["covered_by"]] == [yesterday.id]
    assert view["covered_by"][0]["title"] == "Remind about the phone yesterday" and view["covered_by"][0]["updated"]


def test_the_architect_s_own_graph_and_portfolio_show_labels_not_ids():
    store = _store()
    wo = _goal(store, "Book the dentist", "Find a free slot")
    other = _goal(store, "Water the garden", "Tell the user to water")
    labels, ids, text = A._view(store, wo, history=None, replan=True)
    graph = A._render_existing_graph(wo, ids)
    assert "T1 | Find a free slot" in graph and f"{wo.id}_t0" not in graph and wo.id not in graph
    assert "=== WORK OBJECT W1 ===" in text and other.id not in text and f"{other.id}_t0" not in text
