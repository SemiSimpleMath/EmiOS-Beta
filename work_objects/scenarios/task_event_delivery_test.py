"""Web-free validation of the task EVENT DELIVERY lane (task_runtime.task_events + clock wakes).

The 2026-07-11 verification found the event half of the runtime was dead end-to-end: the compiler
stamped watch_registration payloads nothing registered, nothing subscribed to
signal_router_watch_match, and clock-flavored subscriptions had no deliverer. This proves the lane
with fakes (no live router/scheduler/LLM):

  1. start_task_run REGISTERS the run's watches, instance-scoped (task::<work_id>::<node_id>).
  2. a router watch-match on that key RESUMES the run with the observed event (wake-promotion
     releases the wait) — and a non-task match is ignored.
  3. a TERMINAL run cancels its watches by prefix.
  4. clock.* subscriptions arm one-shots on the timing engine whose fire delivers the SAME
     observed_event string (the time side of a mixed gate).
  5. a template needing watches with NO router available refuses to start (fail loud).

Run:  .venv\\Scripts\\python.exe work_objects\\scenarios\\task_event_delivery_test.py
"""
import os
import sys
import tempfile
from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import app.assistant.tests.test_setup  # noqa: F401,E402  bootstrap DI before project imports

from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator
from app.assistant.utils.pydantic_classes import ToolResult
from work_objects.store import WorkStore
from work_objects.model import utcnow
import app.assistant.task_runtime.task_store as task_store_mod
import app.assistant.task_runtime.task_events as task_events
import app.assistant.task_runtime.task_scheduler as task_scheduler
from app.assistant.task_runtime.entry import start_task_run
from app.assistant.task_runtime.task_store import build_task_scope


class _FakeTool:
    def execute(self, msg):
        args = (getattr(msg, "tool_data", {}) or {}).get("arguments", {}) or {}
        return ToolResult(content=str(args.get("emit", "ok")), result_type="success", data={})


class _FakeRouter:
    def __init__(self):
        self.registered = []
        self.cancelled_prefixes = []

    def register_watch(self, *, request):
        request.validate()
        self.registered.append(request)
        return request

    def cancel_watches_by_prefix(self, *, prefix):
        self.cancelled_prefixes.append(prefix)
        return 1


class _FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, **kw):
        self.jobs[kw.get("id")] = kw


def _inline_spawn(work_id, event_name):
    """Synchronous stand-in for task_events._spawn_resume — the seam that makes the hub-handler
    path deterministic in tests. (Never patch threading.Thread itself: it's the shared module,
    and ThreadPoolExecutor's own worker spawning inside drive() would run inline and deadlock.)"""
    task_events._resume_from_match(work_id, event_name)


def _install_fakes():
    orig_cls, orig_cfg = DI.tool_registry.get_tool_class, DI.tool_registry.get_tool
    DI.tool_registry.get_tool_class = lambda n: _FakeTool if str(n).startswith("fake_") else orig_cls(n)
    DI.tool_registry.get_tool = lambda n: ({"tool_contract": {"metadata": {"min_authority": 50}}}
                                           if str(n).startswith("fake_") else orig_cfg(n))


def _fresh_run_env():
    """A fresh temp store injected as THE task store (resume paths default-resolve it), plus a
    fresh fake router in DI."""
    store = WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="taskev5_"), "t.db"))
    task_store_mod._store = store
    router = _FakeRouter()
    ServiceLocator.register("signal_router", router)
    return store, router


_EV = "signal_router.watch.test_ev"


def _watch_template():
    return {
        "task_id": "evd", "title": "evd", "goal_content": "wait for the watch", "driver": "task_runner",
        "nodes": [
            {"id": "w", "type": "tool", "title": "wait", "wake_kind": "event",
             "payload": {"is_wait": True, "guard": f'"{_EV}" in events_observed',
                         "subscriptions": [_EV],
                         "watch_registration": {"event_name": _EV, "watch_type": "keyword_match",
                                                "predicate": {"keywords": ["test"]}}}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "w", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }


def test_watch_registered_matched_resumed_and_cancelled(monkeypatch_thread=True):
    store, router = _fresh_run_env()
    scope = build_task_scope("evd")

    r = start_task_run(_watch_template(), store=store, scope=scope)
    assert r["status"] == "parked", r
    wid = r["work_id"]

    # 1. registered, instance-scoped, run resolvable from the key + metadata
    assert len(router.registered) == 1, router.registered
    req = router.registered[0]
    wo = store.load(wid)
    w_iid = next(n.id for n in wo.nodes.values() if n.id.split("--")[0] == "w")
    assert req.watch_key == f"task::{wid}::{w_iid}", req.watch_key
    assert req.metadata.get("work_id") == wid
    assert req.event_name == _EV

    # 2. a match on the task:: key resumes the run (inline spawn seam for determinism)
    orig_spawn = task_events._spawn_resume
    task_events._spawn_resume = _inline_spawn
    try:
        task_events.deliver_watch_match(SimpleNamespace(data={
            "watch_key": req.watch_key, "event_name": _EV,
        }))
    finally:
        task_events._spawn_resume = orig_spawn
    wo = store.load(wid)
    assert wo.status == "done", f"match should complete the run, got {wo.status}"
    assert wo.nodes[w_iid].status == "closed"

    # 3. the terminal run cancelled its watches by prefix
    assert f"task::{wid}::" in router.cancelled_prefixes, router.cancelled_prefixes
    print("  test_watch_registered_matched_resumed_and_cancelled: PASS")


def test_non_task_match_is_ignored():
    _fresh_run_env()
    calls = []
    orig_spawn = task_events._spawn_resume
    task_events._spawn_resume = lambda *a: calls.append(a)
    try:
        task_events.deliver_watch_match(SimpleNamespace(data={
            "watch_key": "task_graph::something_else", "event_name": "signal_router.watch.x",
        }))
        task_events.deliver_watch_match(SimpleNamespace(data="not a dict"))
    finally:
        task_events._spawn_resume = orig_spawn
    assert calls == [], f"non-task matches must be ignored, got {calls}"
    print("  test_non_task_match_is_ignored: PASS")


def test_clock_subscription_arms_and_fires_as_observed_event():
    store, _router = _fresh_run_env()
    scope = build_task_scope("clk")
    sub = "clock.local.22_00"
    template = {
        "task_id": "clk", "title": "clk", "goal_content": "wait for 22:00", "driver": "task_runner",
        "nodes": [
            {"id": "w", "type": "tool", "title": "wait", "wake_kind": "event",
             "payload": {"is_wait": True, "guard": f'"{sub}" in events_observed',
                         "subscriptions": [sub]}},
            {"id": "end", "type": "tool", "title": "end", "payload": {"is_end": True}},
        ],
        "edges": [{"src": "w", "dst": "end", "relation": "depends_on"}],
        "preloaded_facts": [],
    }
    fake_sched = _FakeScheduler()
    orig_base = task_scheduler._base_scheduler
    task_scheduler._base_scheduler = lambda: fake_sched
    try:
        r = start_task_run(template, store=store, scope=scope)
        assert r["status"] == "parked", r
        wid = r["work_id"]
        job_id = f"task_wake::{wid}::{sub}"
        assert job_id in fake_sched.jobs, f"clock wake not armed: {list(fake_sched.jobs)}"
        job = fake_sched.jobs[job_id]
        assert list(job["args"]) == [wid, sub], job["args"]
        assert job["run_date"] > utcnow() - timedelta(seconds=5)

        # simulate the one-shot firing exactly as armed: same observed_event string
        task_scheduler._fire_task_wake(wid, sub)
        wo = store.load(wid)
        assert wo.status == "done", f"clock fire should release the gate, got {wo.status}"
    finally:
        task_scheduler._base_scheduler = orig_base
    print("  test_clock_subscription_arms_and_fires_as_observed_event: PASS")


def test_next_clock_fire_parsing():
    now = utcnow()
    f = task_scheduler._next_clock_fire
    assert f("clock.timer.15m", now) == now + timedelta(minutes=15)
    assert f("clock.timer.90s", now) == now + timedelta(seconds=90)
    assert f("clock.timer.2h", now) == now + timedelta(hours=2)
    local = f("clock.local.23_59", now)
    assert local is not None and local > now - timedelta(minutes=1)
    after_past = f("clock.after.00_00", now)   # 00:00 is always past -> fire-if-past
    assert after_past is not None and after_past <= now + timedelta(seconds=3)
    assert f("clock.bogus.xyz", now) is None
    assert f("signal_router.watch.reply", now) is None
    print("  test_next_clock_fire_parsing: PASS")


def test_watches_without_router_refuse_to_start():
    store = WorkStore(path=os.path.join(tempfile.mkdtemp(prefix="taskev5n_"), "t.db"))
    task_store_mod._store = store
    ServiceLocator.register("signal_router", None)   # router unavailable
    try:
        start_task_run(_watch_template(), store=store, scope=build_task_scope("norouter"))
    except RuntimeError as e:
        assert "signal" in str(e).lower() and "router" in str(e).lower(), e
        print("  test_watches_without_router_refuse_to_start: PASS")
        return
    raise AssertionError("expected RuntimeError when watches are needed but no router exists")


if __name__ == "__main__":
    _install_fakes()
    test_watch_registered_matched_resumed_and_cancelled()
    test_non_task_match_is_ignored()
    test_clock_subscription_arms_and_fires_as_observed_event()
    test_next_clock_fire_parsing()
    test_watches_without_router_refuse_to_start()
    print("TASK EVENT DELIVERY: ALL PASS")
