"""The concern door: the one way a concern enters the register.

Pins: code assigns every new concern's id and records its origin; a candidate the judge matches to
an open concern is folded into it (evidence kept, merge journalled), never tracked twice; one that
repeats a closed matter is journalled there and not admitted; duplicates inside one batch become one
concern, and each label follows to where it landed; every candidate must say when it is done; the judge's rules are its
validate.py (corrected once by the agent, agent_runtime/correction.py), and an answer still refused raises, writing
nothing. Candidates come from the brain (persist.
apply_brain_matter); until 2026-10-09 the noticer raised them too. Scratch register; the judge is a fake. Invented
data only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.subconscious import concern_door, persist
from app.assistant.tests.concern_store_helpers import ScratchRegister
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.utils.path_utils import get_app_root

RULES = AgentRegistry()._load_validator(get_app_root() / "assistant" / "agents" / "subconscious" / "concern_door"
                                        / "validate.py")

NOW = datetime.now(timezone.utc)


def _open(cid, title, **kw):
    return {"concern_id": cid, "title": title, "subject": "household", "kind": "anticipated_need",
            "evidence": [{"kind": "calendar_event", "ref": "calendar:x", "snippet": "old"}],
            "notes": "n", "first_observed": (NOW - timedelta(hours=3)).isoformat(), "reinforcement_notes": "", **kw}


def _candidate(label, title, done_when="it is done", **kw):
    return {"label": label, "title": title, "done_when": done_when, "subject": "household",
            "kind": "anticipated_need", "severity": "medium", "horizon": "this_week", "domain_tags": [],
            "addressable_by": ["dayflow_orchestrator"], "notes": "n",
            "evidence": [{"kind": "pod", "ref": f"datapod:email:{label}", "snippet": title}],
            "first_observed": NOW.isoformat(), **kw}


def _judge(*answers):
    """A fake judge answering (candidate, decision, same_as) rows; records what it was shown."""
    seen = []

    def call(payload):
        seen.append(payload)
        return {"decisions": [{"candidate": c, "decision": d, "same_as": s, "reason": "because"}
                              for c, d, s in answers]}
    call.seen = seen
    return call


def _admit(reg, candidates, judge):
    """The brain's way in: plan the candidates (outside the lock), then apply them with its updates."""
    plan = concern_door.plan_admission(candidates, reg.read(), judge=judge)
    return persist.apply_brain_matter([], plan, connect=reg.connect)


def _register(tmp_path, **buckets):
    return ScratchRegister(tmp_path).write({"schema_version": 1, "active": [], "addressing": [],
                                            "resolved": [], "dormant": [], **buckets})


def test_a_new_concern_gets_its_id_from_code(tmp_path):
    reg = _register(tmp_path)
    no_call = _judge()
    admitted = _admit(reg, [_candidate("N1", "Bake sale Friday needs cookies")], no_call)
    assert no_call.seen == [], "nothing to compare with: no model call"
    [c] = reg.read()["active"]
    uuid.UUID(c["concern_id"])
    assert "label" not in c and c["origin"] == "brain" and c["done_when"] == "it is done"
    assert admitted == {"N1": c["concern_id"]}


def test_a_matter_already_tracked_is_folded_into_its_concern(tmp_path):
    reg = _register(tmp_path, active=[_open("c-bake", "PTSA bake sale on Friday")])
    judge = _judge(("N1", "same_open", "C1"))
    _admit(reg, [_candidate("N1", "Bake sale moved to Monday")], judge)
    [c] = reg.read()["active"]
    assert c["concern_id"] == "c-bake"
    assert [e["ref"] for e in c["evidence"]] == ["calendar:x", "datapod:email:N1"]
    assert c["reinforcement_notes"].splitlines()[-1].endswith("] [brain] n"), "the duplicate's note joins it"
    shown = judge.seen[0]
    assert shown["candidates"][0]["label"] == "N1" and shown["existing"][0]["label"] == "C1"


def test_a_matter_already_settled_is_journalled_not_admitted(tmp_path):
    closed = _open("c-flea", "Flea medication due", resolved_at_utc=(NOW - timedelta(days=2)).isoformat(),
                   resolution_reason="the owner gave the dose")
    reg = _register(tmp_path, resolved=[closed])
    _admit(reg, [_candidate("N1", "Flea medication still due")], _judge(("N1", "same_closed", "C1")))
    after = reg.read()
    assert after["active"] == []
    assert after["resolved"][0]["suppressed_remint_count"] == 1
    assert after["resolved"][0]["reinforcement_notes"] == "", "the door's decision is not a note"


def test_what_the_judge_compares_with(tmp_path):
    long_ago = (NOW - timedelta(days=40)).isoformat()
    recent = (NOW - timedelta(days=3)).isoformat()
    register = {"active": [_open("a", "open one")], "addressing": [_open("b", "in hand")],
                "resolved": [_open("r-old", "old", resolved_at_utc=long_ago),
                             _open("r-new", "recent", resolved_at_utc=recent)],
                "dormant": [_open("d", "declined", user_declined_at_utc=long_ago, dormant_at_utc=long_ago)]}
    judge = _judge(("N1", "new", None))
    concern_door.plan_admission([_candidate("N1", "x")], register, judge=judge, now_utc=NOW)
    shown = judge.seen[0]["existing"]
    assert [(e["title"], e["status"]) for e in shown] == [
        ("open one", "active"), ("in hand", "addressing"), ("recent", "resolved"), ("declined", "dormant")]
    assert shown[3]["closed_because"] == "the owner declined it"


def test_duplicates_in_one_batch_become_one_concern_and_labels_follow(tmp_path):
    reg = _register(tmp_path, active=[_open("c-other", "Unrelated")])
    admitted = _admit(reg, [_candidate("N1", "OpenAI API charged $100"), _candidate("N2", "OpenAI API charged $50")],
                      _judge(("N1", "new", None), ("N2", "same_candidate", "N1")))
    active = reg.read()["active"]
    [spend] = [c for c in active if c["title"] == "OpenAI API charged $100"]
    assert len(active) == 2
    assert [e["ref"] for e in spend["evidence"]] == ["datapod:email:N1", "datapod:email:N2"]
    assert admitted == {"N1": spend["concern_id"], "N2": spend["concern_id"]}


def test_every_candidate_must_say_when_it_is_done(tmp_path):
    reg = _register(tmp_path)
    with pytest.raises(ValueError, match="done_when"):
        _admit(reg, [_candidate("N1", "No closing condition", done_when="")], _judge())


def test_an_answer_still_refused_writes_nothing(tmp_path):
    from app.assistant.agent_runtime.correction import AgentOutputInvalid, Problem
    reg = _register(tmp_path, active=[_open("c-bake", "Bake sale")])
    payload = concern_door.build_payload([_candidate("N1", "Bake sale moved")],
                                         [{**_open("c-bake", "Bake sale"), "_status": "active"}])
    [problem] = RULES.validate({"decisions": [{"candidate": "N1", "decision": "same_open", "same_as": "C9"}]},
                               payload)
    assert (problem.item, problem.at, problem.value) == ("N1", "same_as", "C9")
    seen = []

    def refused(payload):
        seen.append(payload)
        raise AgentOutputInvalid("subconscious::concern_door", [problem])
    with pytest.raises(AgentOutputInvalid, match="still invalid"):
        _admit(reg, [_candidate("N1", "Bake sale moved")], refused)
    assert len(seen) == 1 and [c["concern_id"] for c in reg.read()["active"]] == ["c-bake"]


def _given(candidates=("N1", "N2"), existing=(), owner=()):
    return {"candidates": [{"label": x} for x in candidates],
            "existing": [{"label": x, "status": status} for x, status in existing], "owner_labels": list(owner)}


def test_same_candidate_must_point_at_a_new_one():
    assert RULES.ITEM_KEYS == {"decisions": "candidate"}
    problems = RULES.validate(
        {"decisions": [{"candidate": "N1", "decision": "same_candidate", "same_as": "N2"},
                       {"candidate": "N2", "decision": "same_candidate", "same_as": "N1"}]}, _given())
    assert any("names a candidate decided new" in p.why and p.structural for p in problems)


def test_the_door_s_other_rules():
    given = _given(candidates=("N1", "N2", "N3"), existing=(("C1", "active"), ("C2", "resolved")), owner=("N3",))
    problems = RULES.validate({"decisions": [
        {"candidate": "N1", "decision": "new", "same_as": "C1"},
        {"candidate": "N2", "decision": "same_closed", "same_as": "C1"},
        {"candidate": "N3", "decision": "same_closed", "same_as": "C2"},
        {"candidate": "N9", "decision": "new", "same_as": None}]}, given)
    assert {(p.item, p.at, p.structural) for p in problems} == {
        ("N1", "same_as", False), ("N2", "same_as", False), ("N3", "decision", False), ("", "", True)}
    assert [p.why for p in problems if p.item == "N3"] == [
        "it comes from the owner's own words, so it is live: it cannot be same_closed"]
    missing = RULES.validate({"decisions": [{"candidate": "N1", "decision": "new", "same_as": None},
                                            {"candidate": "N1", "decision": "new", "same_as": None}]}, _given())
    assert {(p.item, p.why) for p in missing} == {("N1", "answer each candidate once"),
                                                  ("N2", "not answered; answer every candidate")}


def test_the_judge_prompt_renders_every_field():
    from app.assistant.dayflow_orchestrator import work_context
    payload = concern_door.build_payload(
        [_candidate("N1", "Bake sale moved to Monday")],
        [{**_open("c1", "PTSA bake sale", done_when="cookies delivered"), "_status": "active"}])
    assert payload["owner_labels"] == []
    user = work_context._ENV.get_template("subconscious/concern_door/prompts/user.j2").render(agent_input=payload)
    assert "### [N1] Bake sale moved to Monday" in user and "anchor" not in user
    assert "- done when: it is done" in user and "### [C1] PTSA bake sale (active)" in user
    assert "- done when: cookies delivered" in user and "datapod:email:N1" in user
    system = work_context._ENV.get_template("subconscious/concern_door/prompts/system.j2").render(
        resource_assistant_data={"name": "the assistant"})
    assert "When you are unsure whether two concerns are the same matter, decide new." in system
