"""UTC in the store, local in every prompt.

The store is the boundary: whatever offset a writer hands in is normalized to UTC in the row.
Everything an agent reads renders that back as the user's wall clock, with the day. Before this, an
action taken at 10:02 PM rendered as '09-15 05:02' in the portfolio, and every planner — and the
auditor, repeatedly — read it as a future-dated send; and a wake the architect wrote as
'17:00-07:00' sat in the row with its offset while the state_mover's sat as '+00:00'.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.assistant.control_nodes.work_architect_node import _render_existing_graph
from app.assistant.dayflow_orchestrator.work_architect_apply import _to_dt
from app.assistant.dayflow_orchestrator.work_portfolio import local_stamp, render_work_portfolio
from app.assistant.utils.time_utils import get_local_timezone


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _wo(store, title="Local time WO"):
    wo = store.apply("create_work_object", {"title": title, "goal_content": title,
                                            "satisfied_when_kind": "all_owned_children_done"})
    store.apply("add_node", {"work_id": wo.id, "id": "n1", "type": "subtask",
                             "parent_id": wo.goal_node_id, "title": "Notify at five"})
    return wo.id


class TestTheStoreIsTheUtcBoundary:

    def test_an_offset_wake_is_stored_as_utc(self):
        store = _store()
        wid = _wo(store)
        store.apply("defer_node", {"work_id": wid, "node_id": "n1", "wake_kind": "time",
                                   "wake_at": datetime(2026, 9, 17, 17, 0, tzinfo=timezone(timedelta(hours=-7)))},
                    actor="architect")
        row = store._conn.execute("select wake_at from nodes where work_id=? and id='n1'", (wid,)).fetchone()
        assert row[0] == "2026-09-18T00:00:00+00:00"
        assert store.load(wid).nodes["n1"].wake_at == datetime(2026, 9, 18, 0, 0, tzinfo=timezone.utc)

    def test_an_offset_wake_given_as_a_string_is_stored_as_utc(self):
        store = _store()
        wid = _wo(store)
        store.apply("defer_node", {"work_id": wid, "node_id": "n1", "wake_kind": "time",
                                   "wake_at": "2026-09-17 17:00:00-07:00"}, actor="architect")
        row = store._conn.execute("select wake_at from nodes where work_id=? and id='n1'", (wid,)).fetchone()
        assert row[0] == "2026-09-18T00:00:00+00:00"

    def test_a_naive_architect_wake_is_the_users_local_clock(self):
        """The architect reads 'Current time' as local and writes wakes on that clock."""
        dt = _to_dt("2026-09-17 17:00:00")
        assert dt.tzinfo is not None
        assert dt.utcoffset() == datetime(2026, 9, 17, 17, 0, tzinfo=get_local_timezone()).utcoffset()
        assert _to_dt(datetime(2026, 9, 17, 17, 0)).utcoffset() == dt.utcoffset()


class TestAgentsReadLocalTime:

    def test_local_stamp_carries_the_day_and_the_local_clock(self):
        ts = datetime(2026, 9, 15, 5, 2, tzinfo=timezone.utc)      # 10:02 PM the evening before, in LA
        expected = ts.astimezone(get_local_timezone()).strftime("%a %m-%d %I:%M %p")
        assert local_stamp(ts) == expected
        assert local_stamp(None) == ""

    def test_the_portfolio_renders_actions_and_wakes_in_local_time(self):
        store = _store()
        wid = _wo(store)
        store.apply("record_action", {"work_id": wid, "node_id": "n1", "channel": "ticket",
                                      "target": "user", "summary": "Bedtime reminder", "outcome": "sent"},
                    actor="dayflow_ticket")
        wake = datetime.now(timezone.utc) + timedelta(hours=3)
        store.apply("defer_node", {"work_id": wid, "node_id": "n1", "wake_kind": "time", "wake_at": wake},
                    actor="architect")
        wo = store.load(wid)
        ts = wo.actions[0].ts                       # the store stamps the action at op time, in UTC
        rendered = render_work_portfolio(wo)
        assert local_stamp(ts) in rendered, rendered
        assert ts.strftime("%m-%d %H:%M") not in rendered or local_stamp(ts).endswith(ts.strftime("%I:%M %p"))
        assert local_stamp(wake) in rendered
        assert wake.isoformat() not in rendered

    def test_the_architects_graph_view_renders_wakes_in_local_time(self):
        store = _store()
        wid = _wo(store)
        wake = datetime.now(timezone.utc) + timedelta(hours=3)
        store.apply("defer_node", {"work_id": wid, "node_id": "n1", "wake_kind": "time", "wake_at": wake},
                    actor="architect")
        rendered = _render_existing_graph(store.load(wid))
        assert local_stamp(wake) in rendered
        assert wake.isoformat() not in rendered
