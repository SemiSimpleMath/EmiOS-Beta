"""Cross-room @-addressing (2026-08-01): a room's policy may grant it the right
to address workers running in other rooms via ``mention_addressable_rooms``.

The concrete case: dayflow-orchestrator-spawned work teams register under
``room_id=dayflow_orchestrator``; the owner steering them from master_room used
to get "No active X in this room". master_room's ROOM.md now grants
``mention_addressable_rooms: [dayflow_orchestrator]`` and the active_workers
resolvers expand the sender's room to its addressable set, own room first.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import app.assistant.tests.test_setup  # noqa: F401

from app.assistant.manager_runtime.active_workers import (
    addressable_room_ids,
    find_active_invocation_by_display_name,
    find_active_invocations_by_base_display_name,
    list_active_workers,
)


def _record(invocation_id, display_name, room_id, base=None):
    from datetime import datetime, timezone
    return SimpleNamespace(
        invocation_id=invocation_id,
        manager_name="web_manager",
        display_name=display_name,
        base_display_name=base or display_name,
        room_id=room_id,
        request_id="req",
        started_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


class FakeMam:
    def __init__(self, records):
        self._records = records

    def get_status_payload(self):
        return {"active_invocations": [
            {"invocation_id": r.invocation_id, "display_name": r.display_name,
             "base_display_name": r.base_display_name, "room_id": r.room_id}
            for r in self._records
        ]}

    def find_by_display_name(self, name, *, room_id=None):
        for r in self._records:
            if r.display_name.lower() == name.lower() and (room_id is None or r.room_id == room_id):
                return r
        return None

    def find_by_base_display_name(self, base, *, room_id=None):
        return [r for r in self._records
                if r.base_display_name.lower() == base.lower()
                and (room_id is None or r.room_id == room_id)]


def _with_mam(records):
    from app.assistant.ServiceLocator.service_locator import DI
    return patch.object(type(DI), "mam_instance_manager", FakeMam(records), create=True)


class TestAddressableRoomIds:

    def test_master_room_grant_includes_dayflow(self):
        rooms = addressable_room_ids("master_room")
        assert rooms[0] == "master_room"          # own room first — precedence
        assert "dayflow_orchestrator" in rooms

    def test_room_without_grant_is_own_room_only(self):
        assert addressable_room_ids("dayflow_orchestrator") == ["dayflow_orchestrator"]

    def test_none_room_stays_unfiltered(self):
        assert addressable_room_ids(None) == [None]


class TestCrossRoomResolution:

    def test_master_room_resolves_dayflow_worker(self):
        with _with_mam([_record("inv-1", "Quimby", "dayflow_orchestrator")]):
            found = find_active_invocation_by_display_name("Quimby", room_id="master_room")
        assert found is not None
        assert found["invocation_id"] == "inv-1"
        assert found["room_id"] == "dayflow_orchestrator"

    def test_own_room_worker_wins_over_granted_room(self):
        with _with_mam([
            _record("inv-dayflow", "Quimby", "dayflow_orchestrator"),
            _record("inv-own", "Quimby", "master_room"),
        ]):
            found = find_active_invocation_by_display_name("Quimby", room_id="master_room")
        assert found["invocation_id"] == "inv-own"

    def test_ungranted_room_does_not_see_foreign_workers(self):
        with _with_mam([_record("inv-1", "Quimby", "master_room")]):
            found = find_active_invocation_by_display_name(
                "Quimby", room_id="dayflow_orchestrator")
        assert found is None

    def test_base_name_aggregates_across_addressable_rooms(self):
        with _with_mam([
            _record("inv-1", "Quimby", "master_room", base="Quimby"),
            _record("inv-2", "Quimby_2", "dayflow_orchestrator", base="Quimby"),
        ]):
            matches = find_active_invocations_by_base_display_name(
                "Quimby", room_id="master_room")
        assert {m["invocation_id"] for m in matches} == {"inv-1", "inv-2"}

    def test_list_active_workers_includes_granted_rooms(self):
        with _with_mam([
            _record("inv-1", "Quimby", "dayflow_orchestrator"),
            _record("inv-2", "Phyllis", "slack/other"),
        ]):
            rows = list_active_workers(room_id="master_room")
        assert [r["invocation_id"] for r in rows] == ["inv-1"]


class TestMentionKeyMatching:
    """Display names with spaces/punctuation ("Waffle (graph)") are untypeable
    in @ syntax — the finders accept the mention key and (base stage) head."""

    def test_mention_key_and_head(self):
        from app.assistant.chat_narrator.display_names import mention_head, mention_key
        assert mention_key("Waffle (graph)") == "waffle_graph"
        assert mention_head("Waffle (graph)") == "waffle"
        assert mention_key("Webby_2") == "webby_2"

    def test_exact_finder_accepts_mention_key(self):
        from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
        mam = MAMInstanceManager.__new__(MAMInstanceManager)
        record = _record("inv-g", "Waffle (graph)", "dayflow_orchestrator")
        with patch.object(mam, "list_active", return_value=[record], create=True):
            assert mam.find_by_display_name("waffle_graph") is record
            assert mam.find_by_display_name("Waffle (graph)") is record

    def test_base_finder_accepts_head(self):
        from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
        mam = MAMInstanceManager.__new__(MAMInstanceManager)
        record = _record("inv-g", "Waffle (graph)", "dayflow_orchestrator")
        with patch.object(mam, "list_active", return_value=[record], create=True):
            assert mam.find_by_base_display_name("waffle") == [record]

    def test_manager_for_mention_resolves_key_and_head(self):
        from app.assistant.chat_narrator.display_names import (
            initialize_display_name_registry, manager_for_mention,
        )
        initialize_display_name_registry({
            "work_emi_team_manager": "Waffle (graph)",
            "emi_team_manager": "Waffle",
        })
        try:
            assert manager_for_mention("waffle_graph") == "work_emi_team_manager"
            # bare "waffle": exact display "Waffle" wins over head-match
            assert manager_for_mention("waffle") == "emi_team_manager"
        finally:
            initialize_display_name_registry({})
