from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_EVENT_WAKES = {"event", "user_reply", "signal"}


class StateMoverPersistNode(ControlNode):
    """Applies the state_mover's two outputs to the graph.

      1. ``node_wakes`` — for each node whose awaited external event arrived, clear its event-wait
         (so is_ready / the dispatch pick it up next tick) and attach the arrived content as the
         worker's resume context. This replaces the standalone event_waker.
      2. ``held_work_nodes`` — park the few ready nodes the LLM held for a better moment; promote
         every other ready node to ``actionable``.

    It used to follow state_transition_guard_node, which validated and wrote the state_mover's item
    ``state_mutations``. The item lane is retired (2026-09-16) and the state_mover no longer emits
    mutations, so that node and the ``state_mutations_persisted_tf`` handshake went with it.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        try:
            self._apply_node_wakes()
        except Exception as e:
            logger.error("[%s] node_wakes apply failed: %s", self.name, e)
            logger.debug("[%s] node_wakes exception", self.name, exc_info=True)
        try:
            self._promote_ready_nodes()
        except Exception as e:
            logger.error("[%s] node promotion failed: %s", self.name, e)
            logger.debug("[%s] node promotion exception", self.name, exc_info=True)
        self.blackboard.update_state_value("last_agent", self.name)

    def _promote_ready_nodes(self):
        """Promote architect-born ``proposed`` nodes (and unblocked ``waiting`` ones) to ``actionable`` once
        their gates (time + deps) are clear — the work-object analogue of the items lane's
        ``important_open -> actionable`` move. ONLY an ``actionable`` (state_mover-promoted) node is dispatchable
        by the action_selector; a ``proposed`` node sits in the architect's inbox until promoted here.

        PROMOTE is the safe default. The state_mover LLM may HOLD a few via ``held_work_nodes`` (quiet hours, a
        meeting, the user away) — those are parked until their ``reactivate_at`` instead of promoted. Anything
        the LLM does not list is promoted, so the worst failure mode is "promoted when it could have waited",
        never a stuck node. External-event nodes are woken by ``_apply_node_wakes`` (node_wakes), not here.

        On a WAKE PASS (dayflow_wake_manager; ``triggered_work_node`` set) only that one node is
        considered: the state_mover was shown exactly one candidate, so it is the only node its
        answer can be about. Promoting the rest of the graph from a wake pass would move nodes the
        LLM never saw and could not have held."""
        from work_objects.model import utcnow
        from work_objects.store import FAMILY_BY_TYPE, TRANSITIONS
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        now = utcnow()
        only = str(self.blackboard.get_state_value("triggered_work_node", "") or "").strip()
        holds = {}
        for h in (self.blackboard.get_state_value("held_work_nodes", []) or []):
            if isinstance(h, dict) and str(h.get("task_id") or "").strip():
                holds[str(h["task_id"]).strip()] = h
        promoted, held = [], []
        for s in store.list_work_objects():
            if str(s.get("status") or "").lower() in {"done", "abandoned"}:
                continue
            if only and not only.startswith(f"{s['id']}::"):
                continue
            try:
                wo = store.load(s["id"])
            except Exception as e:
                logger.warning("[%s] promote: %s not loadable: %s", self.name, s.get("id"), e)
                continue
            for n in wo.nodes.values():
                if not wo.is_work_unit(n) or n.status not in {"proposed", "waiting"}:
                    continue
                if only and f"{wo.id}::{n.id}" != only:
                    continue
                if str(getattr(n, "wake_kind", None) or "") in {"event", "signal"}:
                    continue   # external waits are woken via node_wakes when the state_mover matches intake
                # A pre-surface user_reply ask (repair-escalated, or parked here by a HOLD) rides this
                # same promotion toward its surface. An IN-FLIGHT ask is `dispatched` and invisible
                # here by design — it ends by reply/dismissal (materializer -> done) or ticket
                # timeout (sweeper -> failed); there is no re-ask timer.
                if not wo.is_ready(n, now):
                    continue   # time/dep gate not clear — leave it parked
                family = FAMILY_BY_TYPE.get(n.type, "spine")
                if "actionable" not in TRANSITIONS.get(family, {}).get(n.status, set()):
                    continue   # non-dispatchable family (knowledge/question/verification)
                ref = f"{wo.id}::{n.id}"
                hold = holds.get(ref)
                if hold:
                    if self._park_held(store, wo.id, n, hold):
                        held.append(ref)
                        continue
                    # no reactivate_at given -> leave it proposed; re-judged next tick (don't promote now)
                    held.append(ref)
                    continue
                store.apply("set_status", {"work_id": wo.id, "node_id": n.id, "status": "actionable"},
                            actor="state_mover")
                promoted.append(ref)
        if promoted:
            logger.info("[%s] promoted %d ready node(s) -> actionable", self.name, len(promoted))
            self.blackboard.update_state_value("promoted_work_nodes", promoted)
        if held:
            logger.info("[%s] held %d ready node(s) back per state_mover judgment", self.name, len(held))

    def _park_held(self, store, work_id, node, hold) -> bool:
        """Park a state_mover-held ready node until its reactivate_at, so it isn't re-judged every tick.
        Returns True if parked; False if no reactivate_at (caller leaves it proposed to re-judge next tick)."""
        from app.assistant.utils.time_utils import parse_iso_utc
        ra = str(hold.get("reactivate_at") or "").strip()
        wake_at = parse_iso_utc(ra) if ra else None
        if wake_at is None:
            return False
        # State-only: set `waiting` + the wake, nothing else. The hold reason stays in the LLM's
        # held_work_nodes output and the log line — it is NOT written into the node's directive. (Writing it
        # there polluted the worker's task text and ACCUMULATED across successive holds.)
        # A held ASK keeps wake_kind=user_reply (with the pushed-back wake_at) so a reply arriving during
        # the hold is still matched and recorded; everything else parks as a plain time wake.
        wake_kind = "user_reply" if getattr(node, "wake_kind", None) == "user_reply" else "time"
        store.apply("set_status", {"work_id": work_id, "node_id": node.id, "status": "waiting"},
                    actor="state_mover")
        store.apply("defer_node", {"work_id": work_id, "node_id": node.id, "wake_kind": wake_kind,
                                   "wake_at": wake_at, "wake_ref": getattr(node, "wake_ref", None)},
                    actor="state_mover")
        return True

    def _apply_node_wakes(self):
        node_wakes = self.blackboard.get_state_value("node_wakes", []) or []
        if not node_wakes:
            return
        from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
        store = get_dayflow_work_store()
        from uuid import uuid4
        candidates = {c["task_id"]: c for c in
                      (self.blackboard.get_state_value("waiting_work_nodes", []) or [])}
        sources = {s["item_id"]: s for s in
                   (self.blackboard.get_state_value("work_wait_intake", []) or [])}
        woken = []
        for m in node_wakes:
            if not isinstance(m, dict):
                continue
            tid = str(m.get("task_id") or "")
            if "::" not in tid:
                continue
            work_id, node_id = tid.rsplit("::", 1)
            try:
                wo = store.load(work_id)
                node = wo.nodes.get(node_id)
                if node is None or not wo.is_work_unit(node) or getattr(node, "wake_kind", None) not in _EVENT_WAKES:
                    continue  # already woken, or not an event-wait
                if wo.status in {"done", "abandoned"} or node.status not in {"proposed", "waiting"}:
                    continue
                evidence = str(m.get("evidence") or "").strip()
                if not evidence:
                    raise ValueError("External wake requires arrival evidence")
                candidate = candidates.get(tid)
                source = sources.get(str(m.get("source_item_id") or ""))
                if not candidate or not source:
                    raise ValueError("External wake requires a prepared task and exact intake source")
                if (node.wake_kind != candidate["wake_kind"]
                        or node.wake_ref != candidate["waiting_for"]):
                    raise ValueError("External wake condition changed after matching preparation")
                # Gate and attributed evidence commit together, fenced at the pre-LLM snapshot.
                store.apply("batch", {
                    "work_id": work_id, "expected_updated_at": candidate["work_version"],
                    "operations": [
                        {"op": "defer_node", "data": {"node_id": node_id, "wake_kind": None}},
                        {"op": "add_node", "data": {
                            "id": "wake_" + uuid4().hex, "parent_id": node_id,
                            "type": "evidence", "title": source.get("subject") or source["item_id"],
                            "content": evidence, "pod_ref": source.get("pod_id") or None,
                            "payload": {"external_source": dict(source),
                                        "context_role": "external_wake",
                                        "matched_condition": candidate["waiting_for"]}}},
                    ],
                }, actor="state_mover")
                woken.append(tid)
                logger.info("[%s] woke work-object node %s", self.name, tid)
            except Exception as e:
                logger.warning("[%s] could not wake %s: %s", self.name, tid, e)
        if woken:
            self.blackboard.update_state_value("woken_work_nodes", woken)
