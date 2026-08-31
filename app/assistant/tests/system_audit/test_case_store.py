"""System audit register: lifecycle, id-join dedup/attach, regression arming.

The register is the memory of the system's own failures. Identity is ids,
never wording: a signal sharing any bound id with a live case ATTACHES; a new
case whose implicated subsystem was already resolved flags REGRESSED.
"""
from __future__ import annotations

import app.assistant.tests.test_setup  # noqa: F401

import pytest

from sqlalchemy import text

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


def _known_work_ids(monkeypatch, *ids: str) -> None:
    """Substitute the real work-id lookup for this test.

    A unit test must NEVER write fixture rows into the application database. An
    earlier version of this helper did exactly that — CREATE TABLE IF NOT EXISTS
    plus INSERTs through the app session — and in a dev checkout that session is
    the live emi.db. It left three `seed` work objects with NULL created_at, which
    fail WorkObject validation, which made the dayflow planner's portfolio build
    raise on EVERY tick. The planner then saw "(no active work objects)" and
    duplicated work it already had in flight. Substituting the lookup keeps the
    test hermetic and cannot touch any database.
    """
    monkeypatch.setattr(cs, "_known_work_ids", lambda: set(ids))


class TestTranscribedWorkIds:
    """The dedup join is only as good as the ids it joins on. Findings arrive with
    ids an LLM copied out of a dossier, and a single dropped or added character
    silently mints a twin case for a work object that already has a live one — both
    observed in the awaiting_claude backlog."""

    def test_truncated_id_still_attaches_to_the_live_case(self, monkeypatch):
        _known_work_ids(monkeypatch, "work_64a1db7f9fc9")
        a = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"work_ids": ["work_64a1db7f9fc9"]}, summary="first")
        b = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"work_ids": ["work_64a1db7f9fc"]}, summary="one char short")
        assert b == a, "a truncated id must be repaired and attach, not mint a twin"

    def test_overlong_id_still_attaches_to_the_live_case(self, monkeypatch):
        _known_work_ids(monkeypatch, "work_1acedfc7ab4e")
        a = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"work_ids": ["work_1acedfc7ab4e"]}, summary="first")
        b = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"work_ids": ["work_1acedfc7ab4e4"]}, summary="one char long")
        assert b == a

    def test_unresolvable_id_is_kept_not_dropped(self, monkeypatch):
        """Garbage stays bound and loud. Dropping it would destroy the case's only
        anchor, leaving a case that can never be joined to anything."""
        _known_work_ids(monkeypatch, "work_aaaaaaaaaaaa")
        cid = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                           bound_ids={"work_ids": ["work_repeat01"]}, summary="placeholder id")
        row = [c for c in cs.list_cases() if c["id"] == cid][0]
        assert row["bound_ids"]["work_ids"] == ["work_repeat01"]

    def test_ambiguous_prefix_is_not_guessed(self, monkeypatch):
        """Two candidates is not a transcription slip — never pick one."""
        _known_work_ids(monkeypatch, "work_dupe1111", "work_dupe2222")
        cid = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                           bound_ids={"work_ids": ["work_dupe"]}, summary="ambiguous")
        row = [c for c in cs.list_cases() if c["id"] == cid][0]
        assert row["bound_ids"]["work_ids"] == ["work_dupe"]


class TestSubsystemIsOptional:
    """Requiring a subsystem forced a guess on every case, and a confident wrong
    layer buries the component that actually produced the bad input."""

    def test_investigation_without_a_subsystem_is_recorded(self):
        a = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"message_ids": ["mx"]}, summary="a")
        cs.transition(a, "assembled")
        assert cs.mark_investigated(a, preliminary_read="chain", implicated_subsystem=None,
                                    repair_suggestions=[], confidence=0.4) == "investigated"
        row = [c for c in cs.list_cases() if c["id"] == a][0]
        assert row["implicated_subsystem"] is None

    def test_no_subsystem_cannot_claim_a_regression(self):
        """A regression claim needs a named layer to be about."""
        a = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"message_ids": ["m1"]}, summary="a")
        cs.transition(a, "assembled")
        cs.mark_investigated(a, preliminary_read="x", implicated_subsystem="dispatch",
                             repair_suggestions=[], confidence=0.8)
        cs.transition(a, "awaiting_claude")
        cs.transition(a, "resolved", resolution={"commits": ["c1"]})

        b = cs.open_case(trigger_kind="auditor_finding", room_id="r",
                         bound_ids={"message_ids": ["m2"]}, summary="b")
        cs.transition(b, "assembled")
        assert cs.mark_investigated(b, preliminary_read="y", implicated_subsystem=None,
                                    repair_suggestions=[], confidence=0.5) == "investigated"
