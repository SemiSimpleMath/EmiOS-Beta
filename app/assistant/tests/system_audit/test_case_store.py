"""System audit register: lifecycle, id-join dedup/attach, regression arming.

The register is the memory of the system's own failures. Identity is ids,
never wording: a signal sharing any bound id with a live case ATTACHES; a new
case whose implicated subsystem was already resolved flags REGRESSED.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.system_audit import case_store as cs


def _fresh_tables():
    from app.models.base import Base, get_current_engine
    from app.assistant.database.system_audit_case import SystemAuditCase
    Base.metadata.create_all(get_current_engine(), checkfirst=True)
    from app.models.base import get_session
    s = get_session()
    try:
        s.query(SystemAuditCase).delete()
        s.commit()
    finally:
        s.close()


@pytest.fixture(autouse=True)
def clean():
    _fresh_tables()
    yield


class TestLifecycle:
    def test_open_and_walk_the_chain(self):
        cid = cs.open_case(trigger_kind="user_friction", room_id="master_room",
                           bound_ids={"message_ids": ["m1"]}, summary="re-asked after answer",
                           quote={"quote": "why again?", "message_id": "m1"})
        cs.transition(cid, "assembled", dossier_path="x.md")
        cs.transition(cid, "investigated", preliminary_read="chain")
        cs.transition(cid, "awaiting_claude")
        cs.transition(cid, "resolved", resolution={"commits": ["abc"], "disposition": "fixed"})
        row = cs.list_cases(statuses=["resolved"])[0]
        assert row["id"] == cid and row["dossier_path"] == "x.md"

    def test_illegal_transition_raises(self):
        cid = cs.open_case(trigger_kind="user_friction", room_id="master_room",
                           bound_ids={"message_ids": ["m2"]}, summary="s")
        with pytest.raises(ValueError):
            cs.transition(cid, "awaiting_claude")   # open -> awaiting_claude is illegal

    def test_unknown_trigger_kind_raises(self):
        with pytest.raises(ValueError):
            cs.open_case(trigger_kind="vibes", room_id=None, bound_ids={}, summary="s")


class TestIdJoinDedup:
    def test_shared_id_attaches_instead_of_twinning(self):
        a = cs.open_case(trigger_kind="user_friction", room_id="master_room",
                         bound_ids={"message_ids": ["m1"], "ticket_ids": ["t1"]},
                         summary="first", quote={"quote": "wrong", "message_id": "m1"})
        b = cs.open_case(trigger_kind="user_friction", room_id="master_room",
                         bound_ids={"ticket_ids": ["t1"], "work_ids": ["w9"]},
                         summary="second", quote={"quote": "again?!", "message_id": "m3"})
        assert a == b
        row = cs.list_cases()[0]
        assert set(row["bound_ids"]["ticket_ids"]) == {"t1"}
        assert "w9" in row["bound_ids"]["work_ids"]
        assert len(row["friction_quotes"]) == 2

    def test_disjoint_ids_open_separate_cases(self):
        a = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m1"]}, summary="a")
        b = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m2"]}, summary="b")
        assert a != b

    def test_terminal_cases_do_not_capture_new_signals(self):
        a = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m1"]}, summary="a")
        cs.transition(a, "dismissed")
        b = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m1"]}, summary="again")
        assert a != b


class TestRegression:
    def test_resolved_subsystem_recurrence_flags_regressed(self):
        a = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m1"]}, summary="a")
        cs.transition(a, "assembled")
        assert cs.mark_investigated(a, preliminary_read="x", implicated_subsystem="dispatch",
                                    repair_suggestions=[], confidence=0.8) == "investigated"
        cs.transition(a, "awaiting_claude")
        cs.transition(a, "resolved", resolution={"commits": ["c1"]})

        b = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m2"]}, summary="b")
        cs.transition(b, "assembled")
        assert cs.mark_investigated(b, preliminary_read="y", implicated_subsystem="dispatch",
                                    repair_suggestions=[], confidence=0.7) == "regressed"
        row = [c for c in cs.list_cases() if c["id"] == b][0]
        assert row["status"] == "regressed" and row["recurrence_of"] == a

    def test_unresolved_subsystem_does_not_flag(self):
        a = cs.open_case(trigger_kind="user_friction", room_id="r",
                         bound_ids={"message_ids": ["m1"]}, summary="a")
        cs.transition(a, "assembled")
        assert cs.mark_investigated(a, preliminary_read="x", implicated_subsystem="scope",
                                    repair_suggestions=[], confidence=0.9) == "investigated"
