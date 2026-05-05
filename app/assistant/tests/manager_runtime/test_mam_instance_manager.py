"""Tests for MAMInstanceManager — the runtime registry for MAM invocations.

Covers the display-name assignment algorithm (collision + reset
semantics), room-scoped lookups, base-name vs exact lookups, and
basic register/unregister bookkeeping.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.assistant.manager_runtime.mam_instance_manager import (
    MAMInstanceManager,
    ManagerInstanceRecord,
)


@pytest.fixture
def mam():
    """Fresh MAM with disk-publish patched out so tests don't write files."""
    inst = MAMInstanceManager()
    with patch.object(inst, "_publish_status_to_disk"):
        yield inst


def _make_manager_stub(name: str = "stub"):
    m = MagicMock()
    m.name = name
    m.blackboard = MagicMock()
    return m


def _register(mam, *, base="Webby", room="master_room", manager_type="web_manager"):
    return mam.register(
        manager_instance=_make_manager_stub(f"{manager_type}_abcd1234"),
        manager_instance_name=f"{manager_type}_abcd1234",
        manager_name=manager_type,
        base_display_name=base,
        request_id=None,
        room_id=room,
        reply_to=None,
    )


# ── Display-name assignment ───────────────────────────────────────


class TestDisplayNameAssignment:

    def test_first_in_namespace_gets_plain_name(self, mam):
        r = _register(mam, base="Webby", room="master_room")
        assert r.display_name == "Webby"

    def test_second_concurrent_gets_underscore_2(self, mam):
        _register(mam, base="Webby", room="master_room")
        r2 = _register(mam, base="Webby", room="master_room")
        assert r2.display_name == "Webby_2"

    def test_third_concurrent_gets_underscore_3(self, mam):
        _register(mam, base="Webby", room="master_room")
        _register(mam, base="Webby", room="master_room")
        r3 = _register(mam, base="Webby", room="master_room")
        assert r3.display_name == "Webby_3"

    def test_different_rooms_dont_collide(self, mam):
        a = _register(mam, base="Webby", room="master_room")
        b = _register(mam, base="Webby", room="slack::C123")
        # Both plain Webby — different rooms, different namespaces.
        assert a.display_name == "Webby"
        assert b.display_name == "Webby"

    def test_different_bases_dont_collide(self, mam):
        a = _register(mam, base="Webby", room="master_room")
        b = _register(mam, base="Em", room="master_room")
        assert a.display_name == "Webby"
        assert b.display_name == "Em"

    def test_reset_to_plain_when_namespace_empties(self, mam):
        r1 = _register(mam, base="Webby", room="master_room")
        r2 = _register(mam, base="Webby", room="master_room")  # Webby_2
        mam.unregister(r1.invocation_id)
        mam.unregister(r2.invocation_id)
        # Namespace is empty — next register starts fresh.
        r3 = _register(mam, base="Webby", room="master_room")
        assert r3.display_name == "Webby"

    def test_no_reset_when_some_still_active(self, mam):
        _register(mam, base="Webby", room="master_room")  # Webby
        r2 = _register(mam, base="Webby", room="master_room")  # Webby_2
        mam.unregister(r2.invocation_id)
        # Webby still alive → next is _3 (no slot reuse mid-flight).
        r3 = _register(mam, base="Webby", room="master_room")
        assert r3.display_name == "Webby_3"

    def test_no_reset_when_only_low_slot_died(self, mam):
        r1 = _register(mam, base="Webby", room="master_room")  # Webby
        _register(mam, base="Webby", room="master_room")  # Webby_2
        mam.unregister(r1.invocation_id)
        # Only Webby_2 alive — but namespace is non-empty, so highest
        # known suffix is 2, next is _3 (no plain reuse).
        r3 = _register(mam, base="Webby", room="master_room")
        assert r3.display_name == "Webby_3"

    def test_none_room_id_is_its_own_namespace(self, mam):
        a = _register(mam, base="Webby", room=None)
        b = _register(mam, base="Webby", room=None)
        assert a.display_name == "Webby"
        assert b.display_name == "Webby_2"


# ── Lookups ───────────────────────────────────────────────────────


class TestLookups:

    def test_find_by_invocation_id(self, mam):
        r = _register(mam, base="Webby", room="master_room")
        assert mam.find_by_invocation_id(r.invocation_id) is r
        assert mam.find_by_invocation_id("nonexistent") is None

    def test_find_by_display_name_exact_match(self, mam):
        _register(mam, base="Webby", room="master_room")
        r2 = _register(mam, base="Webby", room="master_room")
        found = mam.find_by_display_name("Webby_2", room_id="master_room")
        assert found is r2

    def test_find_by_display_name_case_insensitive(self, mam):
        _register(mam, base="Webby", room="master_room")
        assert mam.find_by_display_name("webby", room_id="master_room") is not None
        assert mam.find_by_display_name("WEBBY", room_id="master_room") is not None

    def test_find_by_display_name_respects_room_scope(self, mam):
        _register(mam, base="Webby", room="master_room")
        # Looking up Webby in a different room → not found.
        assert mam.find_by_display_name("Webby", room_id="slack::C123") is None

    def test_find_by_base_returns_all_in_room(self, mam):
        _register(mam, base="Webby", room="master_room")
        _register(mam, base="Webby", room="master_room")
        _register(mam, base="Webby", room="slack::C123")
        master_webbys = mam.find_by_base_display_name("Webby", room_id="master_room")
        assert len(master_webbys) == 2
        assert {r.display_name for r in master_webbys} == {"Webby", "Webby_2"}

    def test_list_active_sorted_oldest_first(self, mam):
        a = _register(mam, base="Webby", room="master_room")
        b = _register(mam, base="Em", room="master_room")
        rows = mam.list_active()
        assert [r.invocation_id for r in rows] == [a.invocation_id, b.invocation_id]

    def test_list_active_filters_by_room(self, mam):
        _register(mam, base="Webby", room="master_room")
        _register(mam, base="Em", room="slack::C123")
        master = mam.list_active(room_id="master_room")
        slack = mam.list_active(room_id="slack::C123")
        assert len(master) == 1 and master[0].base_display_name == "Webby"
        assert len(slack) == 1 and slack[0].base_display_name == "Em"


# ── Register / unregister bookkeeping ─────────────────────────────


class TestRegistration:

    def test_register_returns_record_with_invocation_id(self, mam):
        r = _register(mam)
        assert r.invocation_id
        assert r.display_name
        assert r.manager_name == "web_manager"
        assert r.room_id == "master_room"

    def test_unregister_removes_record(self, mam):
        r = _register(mam)
        mam.unregister(r.invocation_id)
        assert mam.find_by_invocation_id(r.invocation_id) is None
        assert mam.list_active() == []

    def test_unregister_unknown_is_noop(self, mam):
        # Should not raise.
        mam.unregister("nope")


# ── Status payload (back-compat with old API shape) ───────────────


class TestStatusPayload:

    def test_payload_includes_display_name(self, mam):
        _register(mam, base="Webby", room="master_room")
        _register(mam, base="Webby", room="master_room")  # Webby_2
        payload = mam.get_status_payload()
        assert payload["active_invocation_count"] == 2
        names = {row["display_name"] for row in payload["active_invocations"]}
        assert names == {"Webby", "Webby_2"}

    def test_payload_includes_room_id_and_request_id(self, mam):
        _register(mam, base="Webby", room="master_room")
        payload = mam.get_status_payload()
        row = payload["active_invocations"][0]
        assert row["room_id"] == "master_room"
        assert row["base_display_name"] == "Webby"


# ── Cancel ────────────────────────────────────────────────────────


class TestCancel:

    def test_cancel_sets_cancelled_flag_on_blackboard(self, mam):
        r = _register(mam)
        ok = mam.cancel(r.invocation_id)
        assert ok is True
        # The stub manager's blackboard.update_state_value should have
        # been called with cancelled=True.
        r.manager_instance.blackboard.update_state_value.assert_any_call(
            "cancelled", True,
        )

    def test_cancel_unknown_invocation_returns_false(self, mam):
        assert mam.cancel("nonexistent") is False
