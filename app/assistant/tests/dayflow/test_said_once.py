"""Each fixed explanation and each source is said once in a prompt (owner-approved de-duplication, 2026-11-15).

Measured that day in live prompts: the situation auditor read "Historical failure totals do not request another user
decision ..." 18 times, "The assistant carries this work until every step is done ..." 13 times and the concern
record's order 13 times, once per work object or concern; the day's tasks read a school newsletter three times (under
the concern it raised and under each of the brain's decisions on it); one revision's REASON stood on four tasks; the
day's record listed a work's summary under each decision on it. Pins: the explanation stands once (the reader's
system prompt, or the head of a concern list), the per-item label stays; a source is shown in full where it first
appears and later mentions name that place, keyed by its ref; a shared REASON is printed once. Isolated dayflow store
(conftest); invented data only.
"""
from __future__ import annotations

from app.assistant.agent_runtime.services.prompt_builder import _jinja_env
from app.assistant.dayflow_orchestrator import work_context
from app.assistant.dayflow_orchestrator.work_context import _ENV, render_view

FAILURES = "Historical failure totals"
CARRIED = "until every step is done"
ORDER = "oldest first"
NEWSLETTER = "Winter Formal tickets go on sale Tuesday. Parents may volunteer at the PTSA table."
SYSTEM = dict(date_time="2026-11-15 15:20 PST", resource_assistant_data={"name": "the assistant"},
              resource_user_data={"first_name": "Sam"})


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


def _in_progress():
    return {"state": "in_progress", "replanning": False, "done": [], "dropped": [], "not_achieved": [],
            "unfinished": [], "to_come": []}


def _concern(cid, title, source_ref, attempt_ids, words=NEWSLETTER):
    return {"concern_id": cid, "title": title, "status": "active", "severity": "low", "done_when": "Decided.",
            "source": f'an email, Sun 11-15 09:05 AM: "{words}"', "source_head": "an email, Sun 11-15 09:05 AM",
            "source_ref": source_ref, "expires": "",
            "history": f"[Sun 11-15 09:25 AM] The record of {title}.", "plan": None,
            "attempts": [{"work_id": w, "objective": f"work {w}", "attached_at": "2026-11-15T17:40:00+00:00",
                          "ended": None, "progress": _in_progress(), "judgments": []} for w in attempt_ids]}


def _serve(monkeypatch, served):
    """Each work object serves the concerns `served[work id]` names."""
    monkeypatch.setattr(work_context, "concern_context", lambda wo: [dict(c) for c in served.get(wo.id, [])])


def test_the_auditor_reads_each_explanation_once_in_its_system_prompt(monkeypatch):
    from app.assistant.pipelines.dayflow.utils.situation_snapshot import _build_work_objects
    store = _store()
    a = _goal(store, "Decide on the Winter Formal", "Ask about the formal")
    b = _goal(store, "Volunteer at the PTSA table", "Ask about volunteering")
    c = _goal(store, "Water the garden", "Tell the user to water")
    formal = _concern("c0ffee01aa", "Winter Formal", "email:acct:newsletter", [a.id])
    ptsa = _concern("c0ffee02bb", "PTSA table", "email:acct:newsletter", [b.id])
    garden = _concern("c0ffee03cc", "Garden", "chat:garden", [c.id], words="Water the tomatoes tonight.")
    _serve(monkeypatch, {a.id: [formal], b.id: [ptsa], c.id: [garden]})
    user = _build_work_objects(store)
    system = _jinja_env.get_template("situation_auditor/prompts/system.j2").render(**SYSTEM)
    for phrase in (FAILURES, CARRIED, ORDER):
        assert system.count(phrase) == 1, f"{phrase!r} is said once, in the system prompt"
        assert phrase not in user, f"{phrase!r} is not repeated per work object or concern"
    assert user.count("goal failures judged by finalizer: 0") == 3, "each work object's tally stays"
    assert user.count("  its record:\n") == 3 and user.count("      In progress.\n") == 3, "each label stays"
    # The newsletter behind two concerns: in full under the first work object, named by it under the second.
    assert user.count(NEWSLETTER) == 1
    [named] = [w for w in (a.id, b.id)
               if f"  raised from: an email, Sun 11-15 09:05 AM, shown in full under WORK OBJECT {w}\n" in user]
    assert user.index(f"=== WORK OBJECT {named} ===") < user.index(NEWSLETTER), "named by the work object above"


def test_the_finalizer_reads_each_explanation_once(monkeypatch):
    from app.assistant.control_nodes.work_finalizer_node import WorkFinalizerNode
    from app.assistant.ServiceLocator.service_locator import DI
    store = _store()
    wo = _goal(store, "Decide on the Winter Formal", "Ask about the formal")
    _serve(monkeypatch, {wo.id: [_concern("c0ffee01aa", "Winter Formal", "email:acct:newsletter", [wo.id])]})
    seen = {}

    class Agent:
        def action_handler(self, message):
            seen["message"] = message
            return None

    class Factory:
        def create_agent(self, name):
            return Agent()

    monkeypatch.setattr(DI, "agent_factory", Factory(), raising=False)
    node = next(n for n in wo.nodes.values() if wo.is_work_unit(n))
    WorkFinalizerNode._judge(object.__new__(WorkFinalizerNode), wo, node, None)
    user = seen["message"].task + seen["message"].information
    system = _jinja_env.get_template("dayflow_orchestrator/work_finalizer/prompts/system.j2").render(**SYSTEM)
    for phrase in (FAILURES, CARRIED, ORDER):
        assert system.count(phrase) == 1 and phrase not in user, phrase
    assert user.count("its record:\n") == 2, "the projection and the input each show the concern, with its label"


def test_a_concern_list_says_the_order_and_in_progress_once_at_its_head():
    """A reader whose system prompt does not state them (a worker, the concern planner) reads them once."""
    first = _concern("c0ffee01aa", "Winter Formal", "email:acct:one", ["work_a", "work_b"])
    second = _concern("c0ffee02bb", "PTSA table", "email:acct:two", ["work_c"])
    text = render_view("concern_view", concerns=[first, second])
    assert text.startswith('- A work "In progress" or "On hold" is carried by the assistant until every step is done, '
                           "or abandoned with a reason.\n- A concern's record is oldest first:")
    assert text.count(CARRIED) == 1 and text.count(ORDER) == 1
    assert text.count("  its record:\n") == 2 and text.count("      In progress.\n") == 3
    terse = render_view("concern_view", concerns=[first, second], wording_explained=True)
    assert CARRIED not in terse and ORDER not in terse, "a terse reader's system prompt states them"


def test_the_brain_reads_the_record_order_in_its_system_prompt():
    view = {"label": "C1", "title": "Winter Formal", "status": "active", "subject": "household", "kind": "decision",
            "severity": "low", "horizon": "weeks", "source": "", "done_when": "Decided.", "expires": "",
            "first_observed": "2026-11-15T17:05:00+00:00", "attached_work": [],
            "history": "[Sun 11-15 09:25 AM] The record."}
    user = _ENV.get_template("shared/brain/concern.j2").render(c=view, wording_explained=True)
    assert "- its record:\n[Sun 11-15 09:25 AM] The record." in user and ORDER not in user
    system = _jinja_env.get_template("subconscious/brain/prompts/system.j2").render(**SYSTEM)
    assert "A concern's record is oldest first" in system


def test_one_revision_s_reason_is_printed_once():
    store = _store()
    wo = _goal(store, "Get the settings", "Ask for the settings", "Wait for the reply", "Record the reply", "Ask again")
    reason = "The revised instruction calls for one renewed request; the old wait and its follow-ups are replaced."
    for i in (1, 2, 3):
        store.apply("set_status", {"work_id": wo.id, "node_id": f"{wo.id}_t{i}", "status": "abandoned",
                                   "reason": reason})
    text = render_view("portfolio", work=work_context.work_data(store.load(wo.id)))
    assert text.count(reason) == 1
    assert text.count(f"  REASON: as for {wo.id}_t1 above\n") == 2


def test_the_look_back_shows_each_source_and_each_work_summary_once():
    """The day's tasks and the day's record: an email that raised a concern is in full under the concern; a later
    decision on it, or a second decision on the same email, names where it is; an ended work's summary is listed once."""
    from app.assistant.subconscious import day_tasks
    concerns = [{"ref": "C1", "view": {"source_ref": "email:acct:newsletter"}},
                {"ref": "C2", "view": {"source_ref": None}}]
    line = "The list was sent for review at 8:10 AM, but the owner was not reached."
    work = {"work_id": "work_list", "line": line}
    matters = [
        {"ref": "B1", "at": "2026-11-15T16:15:00+00:00", "new_concerns": [], "events": [
            {"source": "work", "at": "2026-11-15T16:14:00+00:00", "work": dict(work), "decisions": []}]},
        {"ref": "B2", "at": "2026-11-15T17:25:00+00:00", "new_concerns": [], "events": [
            {"source": "email", "source_ref": "email:acct:newsletter", "at": "2026-11-15T17:05:00+00:00",
             "text": NEWSLETTER, "decision": "new_concern", "reason": "A decision is needed."},
            {"source": "work", "at": "2026-11-15T16:22:00+00:00", "work": dict(work), "decisions": []}]},
        {"ref": "B3", "at": "2026-11-15T18:25:00+00:00", "new_concerns": [], "events": [
            {"source": "email", "source_ref": "email:acct:other", "at": "2026-11-15T18:05:00+00:00",
             "text": "Your receipt.", "decision": "no_concern", "reason": "Paid."}]}]
    day_tasks.show_once(matters, concerns)
    assert concerns[0]["view"]["source_shown_under"] is None and concerns[1]["view"]["source_shown_under"] is None
    text = _ENV.get_template("shared/brain/matters_today.j2").render(matters=matters)
    assert NEWSLETTER not in text, "the concern shows it in full"
    assert "- email at" in text and ": shown in full under [C1]\n  the brain: new concern: A decision is needed." in text
    assert "Your receipt." in text, "a source shown nowhere else is shown in full"
    assert text.count(line) == 1 and "- work, ended (summary shown under [B1])\n" in text


def test_a_parked_task_s_source_is_shown_once_to_the_state_mover():
    from app.assistant.control_nodes.state_mover_prep_node import label_state_mover_view

    class BB(dict):
        def get_state_value(self, k, d=None):
            return self.get(k, d)

        def update_state_value(self, k, v):
            self[k] = v

    source = {"item_id": "dayflow_email:acct:newsletter", "source_type": "email", "summary": NEWSLETTER,
              "excerpt": "The full newsletter.", "pod_id": "datapod:email:1"}
    waiting = [{"task_id": f"work_a::t{i}", "waiting_for": "a reply", "waiting_since": "Sun 11-15 09:30 AM",
                "context": "Capture the reply", "objective": "Decide", "sources": [dict(source)]} for i in (1, 2)]
    bb = BB(waiting_work_nodes=waiting, work_wait_intake=[dict(source, acted_on_by=[])], ready_work_nodes=[])
    label_state_mover_view(bb)
    text = _jinja_env.get_template("dayflow_orchestrator/state_mover/prompts/user.j2").render(
        waiting_work_nodes=bb["waiting_work_nodes"], work_wait_intake=bb["work_wait_intake"],
        state_mover_ids=bb["state_mover_ids"], date_time="now")
    assert text.count(NEWSLETTER) == 1 and text.count("The full newsletter.") == 1
    assert text.count("- [S1] source | type: email\n  shown in full above, under [T1]\n") == 2, "the second task and the arrival"
