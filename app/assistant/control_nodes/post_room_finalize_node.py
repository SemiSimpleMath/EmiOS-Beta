from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Dict, List

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import get_max_short_id, resolve_short_id, write_dayflow_item
from app.assistant.dayflow_orchestrator.state_store import (
    DAYFLOW_ROOM_ID,
    build_plan_synopsis_messages,
    get_dayflow_items,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import local_to_utc

logger = get_logger(__name__)


def _local_time_to_utc_iso(value: str) -> str:
    """Parse an ISO 8601 datetime (with timezone) from LLM output to UTC ISO for storage.

    The LLM is instructed to output ISO 8601 with timezone offset
    (e.g. 2026-04-10T16:55:00-07:00). We parse it and convert to UTC.
    Falls back to local_to_utc for legacy formats without offsets.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        from datetime import datetime, timezone
        parsed = datetime.fromisoformat(value.strip())
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc).isoformat()
        # Fallback for naive datetimes — assume local timezone.
        return local_to_utc(value.strip()).isoformat()
    except Exception as e:
        logger.error("Could not convert time to UTC: %r (%s)", value, e)
        logger.debug("time to UTC conversion exception details", exc_info=True)
        raise


def _normalize_reactivate_at(items: List[Dict[str, Any]]) -> None:
    """Map LLM-facing ``reactivate_at`` to internal ``reactivate_at_utc``.

    Mutates dicts in place. Handles both mutation dicts and plan step dicts.
    """
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.pop("reactivate_at", None)
        if raw and isinstance(raw, str) and raw.strip():
            item["reactivate_at_utc"] = _local_time_to_utc_iso(raw)


def _build_action_log_item_id(*, item_id: str, action_type: str, now_utc: datetime) -> str:
    seed = f"{item_id}|{action_type}|{now_utc.isoformat()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"action_log:{digest}"


def _extract_full_result_text(action_result_event: Dict[str, Any]) -> str:
    """Pull the FULL manager-result text out of an action_result_event.
    Never truncates. Prefers final_answer_answer (the long structured
    report), falls back to the rendered information field, then to a
    JSON dump of the whole payload — but always emits the entire
    available text so the formatter agent sees what the manager
    actually produced.
    """
    if not isinstance(action_result_event, dict):
        return ""
    payload = action_result_event.get("result_payload")
    if isinstance(payload, dict):
        # final_answer_answer is the long markdown report — exactly
        # what we want to hand the formatter for it to digest.
        for key in ("final_answer_answer", "content"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v
        # No top-level markdown — include result_summary if present
        # plus any structured fields, JSON-dumped (no truncation).
        try:
            import json as _json
            return _json.dumps(payload, ensure_ascii=False, default=str, indent=2)
        except Exception:
            return str(payload)
    info = action_result_event.get("information")
    if isinstance(info, str) and info.strip():
        return info
    return str(action_result_event.get("action_type") or "")


def _format_compact_outcome(*, task: str, action: str, full_result: str) -> str:
    """Send the full manager-result text to dayflow_orchestrator::result_formatter
    and return its compact 1-3 line outcome. Synchronous — adds 1-3s
    latency to post_room_finalize_node but the result lands directly
    on the task and propagates to the strategic_planner's next tick.

    The agent receives the full untruncated full_result. NEVER slice
    it before this call. See feedback_no_truncated_tool_results.md.

    Failure modes:
      - Agent unavailable / errors → fall back to a hedged stub like
        "(formatter unavailable; see action_log)" so downstream consumers
        don't get a 3KB blast.
      - full_result is empty → return empty (caller handles).
    """
    if not full_result or not full_result.strip():
        return ""

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message
    except Exception as e:
        logger.warning("[post_room_finalize] result_formatter dependencies unavailable: %s", e)
        return "(formatter unavailable; see action_log)"

    agent = DI.agent_factory.create_agent("dayflow_orchestrator::result_formatter")
    if agent is None:
        logger.warning("[post_room_finalize] dayflow_orchestrator::result_formatter not registered")
        return "(formatter unavailable; see action_log)"

    agent_input = {
        "task": task or "",
        "action": action or "",
        "full_result": full_result,
    }
    try:
        result = agent.action_handler(Message(agent_input=agent_input))
    except Exception as e:
        logger.error(
            "[post_room_finalize] result_formatter agent crashed: %s", e,
            exc_info=True,
        )
        return "(formatter errored; see action_log)"

    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return "(formatter returned no data; see action_log)"

    outcome = str(data.get("outcome") or "").strip()
    open_qs = data.get("open_questions") or []
    needs_followup = bool(data.get("needs_followup"))
    followup_hint = str(data.get("followup_hint") or "").strip()

    parts = [outcome] if outcome else ["(formatter returned no outcome)"]
    if open_qs and isinstance(open_qs, list):
        for q in open_qs[:3]:
            qs = str(q or "").strip()
            if qs:
                parts.append(f"  ? {qs}")
    if needs_followup and followup_hint:
        parts.append(f"  → {followup_hint}")
    return "\n".join(parts)


class PostRoomFinalizeNode(ControlNode):
    """
    Deterministic post-room hook.

    Persists state mutations, spawned items, plan steps, and planner mutations
    produced during the run back to unified_log_2026.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        now_utc = datetime.now(timezone.utc)

        from app.assistant.dayflow_orchestrator.contracts import (
            validate_dayflow_items,
            validate_mutations,
            validate_planned_tasks,
        )

        existing_items = get_dayflow_items()
        validate_dayflow_items(existing_items, f"{self.name}::existing_dayflow_items")

        all_known_items = list(existing_items)
        existing_meta_by_id: Dict[str, Dict[str, Any]] = {}
        for item in all_known_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id:
                existing_meta_by_id[item_id] = meta
            short_id = str(meta.get("short_id") or "").strip()
            if short_id:
                existing_meta_by_id[short_id] = meta

        planned_tasks = self.blackboard.get_state_value("planned_tasks", []) or []
        validate_planned_tasks(planned_tasks, f"{self.name}::planned_tasks")
        if not isinstance(planned_tasks, list):
            raise ValueError(f"[{self.name}] planned_tasks must be a list.")
        _normalize_reactivate_at(planned_tasks)

        planned_task_messages = self._build_planned_task_messages(
            planned_tasks=planned_tasks,
            existing_meta_by_id=existing_meta_by_id,
            now_utc=now_utc,
        )
        if planned_task_messages:
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch as _write_batch
            task_dicts = []
            for msg in planned_task_messages:
                meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else {}
                meta.setdefault("item_id", msg.id)
                meta.setdefault("summary", msg.content or "")
                meta.setdefault("source_type", meta.get("source_type", "plan_task"))
                task_dicts.append(meta)
            _write_batch(task_dicts, caller=self.name)
            logger.info("[%s] persisted %d planned task item(s).", self.name, len(planned_task_messages))
            for msg in planned_task_messages:
                all_known_items.append(
                    {
                        "id": msg.id,
                        "metadata": msg.metadata or {},
                        "content": msg.content,
                        "sender": msg.sender,
                    }
                )

        mutations = self.blackboard.get_state_value("state_mutations", [])
        if mutations is None:
            mutations = []
        validate_mutations(mutations, f"{self.name}::state_mutations")
        if not isinstance(mutations, list):
            raise ValueError(f"[{self.name}] state_mutations must be a list, got {type(mutations).__name__}.")

        acted_on_ids_raw = self.blackboard.get_state_value("acted_on_item_ids", [])
        if acted_on_ids_raw is None:
            acted_on_ids_raw = []
        if not isinstance(acted_on_ids_raw, list):
            raise ValueError(f"[{self.name}] acted_on_item_ids must be a list, got {type(acted_on_ids_raw).__name__}.")
        acted_on_ids = [resolve_short_id(str(rid or "").strip()) for rid in acted_on_ids_raw]

        dispatch_records_raw = self.blackboard.get_state_value("active_dispatch_records", [])
        if dispatch_records_raw is None:
            dispatch_records_raw = []
        if not isinstance(dispatch_records_raw, list):
            raise ValueError(
                f"[{self.name}] active_dispatch_records must be a list, got {type(dispatch_records_raw).__name__}."
            )
        dispatch_records: List[Dict[str, Any]] = []
        for idx, raw in enumerate(dispatch_records_raw):
            if not isinstance(raw, dict):
                raise ValueError(f"[{self.name}] active_dispatch_records[{idx}] must be an object.")
            dispatch_id = str(raw.get("dispatch_id") or "").strip()
            dispatch_item_id = str(raw.get("dispatch_item_id") or "").strip()
            acted_on_item_id = str(raw.get("acted_on_item_id") or "").strip()
            action_type = str(raw.get("action_type") or "").strip()
            if not dispatch_id or not dispatch_item_id or not acted_on_item_id or not action_type:
                raise ValueError(
                    f"[{self.name}] active_dispatch_records[{idx}] missing required provenance fields."
                )
            dispatch_records.append(raw)

        action_results = self.blackboard.get_state_value("action_result_events", [])
        action_taken = isinstance(action_results, list) and len(action_results) > 0

        if action_taken and not dispatch_records:
            raise ValueError(f"[{self.name}] action_result_events present but active_dispatch_records is empty.")

        if dispatch_records and not action_taken:
            raise ValueError(f"[{self.name}] active_dispatch_records present but action_result_events is empty.")

        dispatch_acted_ids = {
            str(r.get("acted_on_item_id") or "").strip()
            for r in dispatch_records
            if isinstance(r, dict)
        }
        dispatch_acted_ids.discard("")

        # Build a compact task-update line by sending the FULL manager
        # result to dayflow_orchestrator::result_formatter. NO TRUNCATION
        # of the source — the formatter agent reads the whole thing and
        # decides what's important. Char-slicing here would silently drop
        # specifics (dates, IDs, addresses) at an arbitrary boundary.
        # See feedback_no_truncated_tool_results.md.
        #
        # The full manager output stays on the action_result_event
        # (result_payload) regardless, so any later agent that wants to
        # drill into the raw report still has it.
        execution_result_summary = ""
        if action_taken:
            first_result = action_results[0] if action_results else {}
            full_result_text = _extract_full_result_text(first_result)
            task_text = str(first_result.get("task") or "").strip()
            action_type = str(first_result.get("action_type") or "").strip()
            execution_result_summary = _format_compact_outcome(
                task=task_text,
                action=action_type,
                full_result=full_result_text,
            )

        if acted_on_ids and dispatch_acted_ids and set(acted_on_ids) != dispatch_acted_ids:
            raise ValueError(
                f"[{self.name}] acted_on_item_ids mismatch between selector and dispatch records: "
                f"selector={sorted(set(acted_on_ids))}, dispatch={sorted(dispatch_acted_ids)}"
            )
        if not acted_on_ids and dispatch_acted_ids:
            acted_on_ids = sorted(dispatch_acted_ids)

        if acted_on_ids:
            existing_mutation_ids = {str(m.get("item_id") or "").strip() for m in mutations if isinstance(m, dict)}
            for raw_id in acted_on_ids:
                item_id = str(raw_id or "").strip()
                if not item_id or item_id in existing_mutation_ids:
                    continue
                mutations.append({
                    "item_id": item_id,
                    "from_state": "",
                    "to_state": "closed",
                    "reason": "action_completed",
                })
            logger.info(
                "[%s] action feedback: %d acted_on_item_ids, action_results=%d, dispatch_records=%d.",
                self.name,
                len(acted_on_ids),
                len(action_results) if isinstance(action_results, list) else 0,
                len(dispatch_records),
            )

        # Stamp execution results onto plan steps that were just acted on.
        if acted_on_ids and execution_result_summary:
            self._attach_execution_results(
                all_known_items,
                acted_on_ids=acted_on_ids,
                result_summary=execution_result_summary,
                now_utc=now_utc,
            )

        if acted_on_ids and action_taken:
            self._persist_action_selector_action_logs(
                all_known_items=all_known_items,
                dispatch_records=dispatch_records,
                action_results=action_results,
                now_utc=now_utc,
            )
            # Write simple action log messages for the planner's history view.
            from app.assistant.dayflow_orchestrator.state_store import write_action_log
            # Build item_id → short_id reverse mapping from loaded items.
            _log_real_to_short: Dict[str, str] = {}
            for _item in all_known_items:
                if not isinstance(_item, dict):
                    continue
                _m = get_meta(_item)
                _sid = str(_m.get("short_id") or "").strip()
                _iid = str(_m.get("item_id") or _item.get("id") or "").strip()
                if _sid and _iid:
                    _log_real_to_short[_iid] = _sid
            for record in dispatch_records:
                dispatch_id = str(record.get("dispatch_id") or "").strip()
                item_id = str(record.get("acted_on_item_id") or "").strip()
                raw_task_id = str(record.get("task_id") or item_id)
                task_id = _log_real_to_short.get(raw_task_id, raw_task_id)
                plan_id = str(record.get("plan_id") or "")
                task_summary = str(record.get("task_summary") or item_id)
                action_type = str(record.get("action_type") or "").strip()
                write_action_log(
                    task_id=task_id,
                    plan_id=plan_id,
                    event_type="dispatch",
                    summary=f"Dispatched to {action_type}: {task_summary}",
                    idempotency_key=f"{dispatch_id}|dispatch",
                )
                write_action_log(
                    task_id=task_id,
                    plan_id=plan_id,
                    event_type="result",
                    summary=f"Result: {execution_result_summary}",
                    detail=execution_result_summary,
                    idempotency_key=f"{dispatch_id}|result",
                )

        # The state_mover's mutations may have been persisted already by
        # state_transition_guard_node (flagged via state_mutations_persisted_tf).
        # But post_room adds its OWN mutations for acted_on tasks — those must
        # always be persisted regardless of the flag.
        state_mover_already_persisted = bool(
            self.blackboard.get_state_value("state_mutations_persisted_tf", False)
        )

        # Separate state_mover mutations (from blackboard) from post_room's own.
        state_mover_mutation_ids = set()
        if state_mover_already_persisted:
            sm_raw = self.blackboard.get_state_value("state_mutations", []) or []
            state_mover_mutation_ids = {
                str(m.get("item_id") or "").strip()
                for m in sm_raw if isinstance(m, dict)
            }

        own_mutations = [
            m for m in mutations
            if str(m.get("item_id") or "").strip() not in state_mover_mutation_ids
        ]
        skipped = len(mutations) - len(own_mutations)

        mutated_count = skipped  # count already-persisted as "handled"
        if own_mutations:
            _normalize_reactivate_at(own_mutations)
            for mut in own_mutations:
                mid = str(mut.get("item_id") or "").strip()
                to_state = str(mut.get("to_state") or "").strip()
                reason = str(mut.get("reason") or "").strip()
                if mid and to_state:
                    extra = {}
                    for k in ("wait_reason", "reactivate_at_utc", "priority_on_wake"):
                        if mut.get(k):
                            extra[k] = mut[k]
                    for k in ("wake_signals", "blocked_by"):
                        if isinstance(mut.get(k), list) and mut[k]:
                            extra[k] = mut[k]
                    write_dayflow_item(mid, state=to_state, updates=extra, reason=reason, caller=self.name)
            mutated_count += len(own_mutations)
            logger.info(
                "[%s] applied %d post-room mutation(s), %d skipped (state_mover already persisted).",
                self.name, len(own_mutations), skipped,
            )
        elif skipped:
            logger.info(
                "[%s] skipping %d mutation(s) — all already persisted by state_transition_guard_node.",
                self.name, skipped,
            )

        spawned_count = len(planned_task_messages)
        plan_count = len(planned_task_messages)

        plan_synopsis_count = 0
        plan_synopses_raw = self.blackboard.get_state_value("plan_synopses", [])
        if plan_synopses_raw is None:
            plan_synopses_raw = []
        if not isinstance(plan_synopses_raw, list):
            raise ValueError(
                f"[{self.name}] plan_synopses must be a list, got {type(plan_synopses_raw).__name__}."
            )
        if plan_synopses_raw:
            # Build lookup of existing synopses for safety-net diff.
            existing_synopses: Dict[str, Dict[str, Any]] = {}
            for item in all_known_items:
                if not isinstance(item, dict):
                    continue
                meta = get_meta(item)
                if str(meta.get("source_type") or "").strip().lower() != "plan_synopsis":
                    continue
                sid = str(meta.get("plan_id") or "").strip()
                if sid:
                    existing_synopses[sid] = meta

            changed_synopses: List[Dict[str, Any]] = []
            for s in plan_synopses_raw:
                if not isinstance(s, dict):
                    continue
                plan_id = str(s.get("plan_id") or "").strip()
                declared_changed = bool(s.get("changed", False))

                existing = existing_synopses.get(plan_id)
                if existing is None:
                    # New plan — always persist.
                    changed_synopses.append(s)
                    continue

                if declared_changed:
                    changed_synopses.append(s)
                    continue

                # Safety net: check if objective or step_outline substantially changed
                # even though planner said changed=false.
                old_objective = str(existing.get("objective") or "").strip()
                new_objective = str(s.get("objective") or "").strip()
                old_steps = existing.get("step_outline") or []
                new_steps = s.get("step_outline") or []
                if old_objective != new_objective or old_steps != new_steps:
                    logger.warning(
                        "[%s] plan synopsis %r marked changed=false but objective or "
                        "step_outline differ — persisting anyway.",
                        self.name, plan_id,
                    )
                    changed_synopses.append(s)

            if changed_synopses:
                synopsis_messages = build_plan_synopsis_messages(changed_synopses)
                if synopsis_messages:
                    from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch as _write_batch
                    syn_dicts = []
                    for msg in synopsis_messages:
                        meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else {}
                        meta.setdefault("item_id", msg.id)
                        meta.setdefault("summary", msg.content or "")
                        meta.setdefault("source_type", "plan_synopsis")
                        syn_dicts.append(meta)
                    _write_batch(syn_dicts, caller=self.name)
                    plan_synopsis_count = len(syn_dicts)
            logger.info(
                "[%s] plan synopses: %d emitted, %d changed, %d persisted.",
                self.name, len(plan_synopses_raw), len(changed_synopses), plan_synopsis_count,
            )

        completed_plan_ids_raw = self.blackboard.get_state_value("completed_plan_ids", [])
        if completed_plan_ids_raw is None:
            completed_plan_ids_raw = []
        if not isinstance(completed_plan_ids_raw, list):
            raise ValueError(
                f"[{self.name}] completed_plan_ids must be a list, got {type(completed_plan_ids_raw).__name__}."
            )
        completed_set = {str(pid or "").strip() for pid in completed_plan_ids_raw if str(pid or "").strip()}
        if completed_set:
            self._close_completed_plans(all_known_items, completed_set, now_utc)

        closed_task_ids_raw = self.blackboard.get_state_value("closed_task_ids", [])
        if closed_task_ids_raw is None:
            closed_task_ids_raw = []
        if not isinstance(closed_task_ids_raw, list):
            raise ValueError(
                f"[{self.name}] closed_task_ids must be a list, got {type(closed_task_ids_raw).__name__}."
            )
        cancelled_set = {str(tid or "").strip() for tid in closed_task_ids_raw if str(tid or "").strip()}
        if cancelled_set:
            self._cancel_tasks(all_known_items, cancelled_set, now_utc)

        self.blackboard.update_state_value("post_room_mutated_count", mutated_count)
        self.blackboard.update_state_value("post_room_spawned_count", spawned_count)
        self.blackboard.update_state_value("post_room_plan_step_count", plan_count)
        self.blackboard.update_state_value("post_room_plan_synopsis_count", plan_synopsis_count)
        self.blackboard.update_state_value("post_room_processed_at_utc", now_utc.isoformat())
        self.blackboard.update_state_value("last_agent", self.name)

    def _build_planned_task_messages(
        self,
        *,
        planned_tasks: List[Dict[str, Any]],
        existing_meta_by_id: Dict[str, Dict[str, Any]],
        now_utc: datetime,
    ) -> List[Message]:
        import uuid as _uuid

        max_id = get_max_short_id()

        # Determine which plan_ids are new from the planner's is_new flag
        # on synopses. Tasks under new plans always get fresh IDs — the
        # planner uses placeholder indices ("1", "2", ...) when creating.
        plan_synopses_raw = self.blackboard.get_state_value("plan_synopses", []) or []
        new_plan_ids: set[str] = set()
        for syn in plan_synopses_raw:
            if isinstance(syn, dict) and syn.get("is_new"):
                pid = str(syn.get("plan_id") or "").strip()
                if pid:
                    new_plan_ids.add(pid)

        # Pass 1: resolve planner refs to canonical item_ids, assign short_ids
        # to new tasks, and build a planner_ref → item_id mapping for
        # dependency resolution.
        planner_ref_to_item_id: Dict[str, str] = {}
        new_short_id_map: Dict[str, str] = {}  # short_id → item_id for newly assigned
        resolved_tasks: List[tuple] = []  # (raw, item_id, existing_meta)

        for idx, raw in enumerate(planned_tasks):
            if not isinstance(raw, dict):
                raise ValueError(f"[{self.name}] planned_tasks[{idx}] must be an object.")
            planner_ref = str(raw.get("task_id") or "").strip()
            if not planner_ref:
                raise ValueError(f"[{self.name}] planned_tasks[{idx}] missing task_id.")
            summary = str(raw.get("task") or "").strip()
            if not summary:
                raise ValueError(f"[{self.name}] planned_tasks[{idx}] missing task.")

            task_plan_id = str(raw.get("plan_id") or "").strip()
            is_new_plan = task_plan_id in new_plan_ids

            # Only look up existing items if this task belongs to a known plan.
            existing_meta = {}
            if not is_new_plan:
                existing_meta = existing_meta_by_id.get(planner_ref, {})
            is_existing = bool(existing_meta)

            if is_existing:
                # Planner is updating an existing task — use its canonical item_id.
                item_id = str(existing_meta.get("item_id") or "").strip()
                if not item_id:
                    raise ValueError(
                        f"[{self.name}] existing task matched by planner ref {planner_ref!r} "
                        "but has no item_id in metadata."
                    )
            else:
                # New task — generate a stable unique item_id and assign short_id.
                item_id = f"task:{_uuid.uuid4().hex[:12]}"
                max_id += 1
                new_sid = str(max_id)
                new_short_id_map[new_sid] = item_id

            planner_ref_to_item_id[planner_ref] = item_id
            resolved_tasks.append((raw, item_id, existing_meta))

        # Pass 2: build Messages with resolved item_ids and dependencies.
        messages: List[Message] = []
        for raw, item_id, existing_meta in resolved_tasks:
            summary = str(raw.get("task") or "").strip()

            # Resolve dependencies from planner refs to canonical item_ids.
            raw_deps = raw.get("dependencies")
            if isinstance(raw_deps, list) and raw_deps:
                resolved_deps = []
                for dep_ref in raw_deps:
                    dep_ref = str(dep_ref or "").strip()
                    if not dep_ref:
                        continue
                    resolved_dep = planner_ref_to_item_id.get(dep_ref)
                    if not resolved_dep:
                        # Try existing meta lookup (may be a short_id or item_id).
                        dep_meta = existing_meta_by_id.get(dep_ref, {})
                        resolved_dep = str(dep_meta.get("item_id") or dep_ref).strip()
                    resolved_deps.append(resolved_dep)
                blocked_by = resolved_deps
            else:
                blocked_by = (
                    existing_meta.get("blocked_by")
                    if isinstance(existing_meta.get("blocked_by"), list)
                    else []
                )

            metadata: Dict[str, Any] = dict(existing_meta)
            metadata.update(
                {
                    "item_id": item_id,
                    "source_type": str(existing_meta.get("source_type") or "plan_task"),
                    "event_type": str(existing_meta.get("event_type") or "planned_task"),
                    "created_at": str(existing_meta.get("created_at") or now_utc.isoformat()),
                    "summary": summary,
                    "importance": str(existing_meta.get("importance") or "medium"),
                    "actionability": "actionable",
                    "state": str(existing_meta.get("state") or "important_open"),
                    "state_reason": str(existing_meta.get("state_reason") or "planner_task"),
                    "last_reviewed_at": now_utc.isoformat(),
                    "cooldown_until": None,
                    "linked_item_ids": existing_meta.get("linked_item_ids")
                    if isinstance(existing_meta.get("linked_item_ids"), list)
                    else [],
                    "plan_id": str(raw.get("plan_id") or existing_meta.get("plan_id") or "").strip(),
                    "blocked_by": blocked_by,
                    "wake_signals": raw["wake_signals"]
                    if "wake_signals" in raw and isinstance(raw["wake_signals"], list)
                    else (
                        existing_meta.get("wake_signals")
                        if isinstance(existing_meta.get("wake_signals"), list)
                        else []
                    ),
                    "reactivate_at_utc": str(raw["reactivate_at_utc"]).strip()
                    if raw.get("reactivate_at_utc")
                    else str(existing_meta.get("reactivate_at_utc") or "").strip(),
                    "wait_reason": str(raw["wait_reason"]).strip()
                    if raw.get("wait_reason")
                    else str(existing_meta.get("wait_reason") or "").strip(),
                    "priority_on_wake": str(raw["priority_on_wake"]).strip()
                    if raw.get("priority_on_wake")
                    else str(existing_meta.get("priority_on_wake") or "").strip(),
                }
            )

            if not metadata.get("short_id"):
                # New task — short_id was assigned in pass 1.
                reverse = {v: k for k, v in new_short_id_map.items()}
                metadata["short_id"] = reverse.get(item_id, "")

            messages.append(
                Message(
                    id=item_id,
                    data_type=str(metadata.get("data_type") or "dayflow_input_item"),
                    sub_data_type=metadata.get("sub_data_type") or ["dayflow_orchestrator", "planned_task", "dayflow_task"],
                    sender="strategic_planner",
                    content=summary,
                    timestamp=now_utc,
                    room_id=DAYFLOW_ROOM_ID,
                    metadata=metadata,
                )
            )

        return messages

    def _persist_action_selector_action_logs(
        self,
        *,
        all_known_items: List[Dict[str, Any]],
        dispatch_records: List[Dict[str, Any]],
        action_results: List[Dict[str, Any]],
        now_utc: datetime,
    ) -> None:
        """Persist deterministic action results and close dispatch markers."""
        if not dispatch_records:
            return

        item_meta_by_id: Dict[str, Dict[str, Any]] = {}
        for item in all_known_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id:
                item_meta_by_id[item_id] = meta

        result = action_results[0] if action_results else {}
        action_type = str(result.get("action_type") or "switchboard_action").strip() or "switchboard_action"
        info_text = str(result.get("information") or "").strip()
        payload_text = str(result.get("result_payload") or "").strip()
        raw_outcome = info_text or payload_text or "Action executed via switchboard."
        outcome = " ".join(raw_outcome.split())

        reasoning = str(self.blackboard.get_state_value("reasoning", "") or "").strip()

        created_ticket_id = ""
        result_payload = result.get("result_payload")
        if isinstance(result_payload, dict):
            created_ticket_id = str(result_payload.get("ticket_id") or "").strip()
        if not created_ticket_id:
            result_content = str(result.get("information") or "").strip()
            if "ticket" in result_content.lower() and "Created" in result_content:
                for token in result_content.split():
                    if "dayflow_orchestrator_" in token.rstrip("."):
                        created_ticket_id = token.rstrip(".")
                        break

        from app.assistant.utils.time_utils import get_local_timezone
        local_tz = get_local_timezone()
        created_at_local = now_utc.astimezone(local_tz).strftime("%I:%M %p")

        result_messages: List[Message] = []
        dispatch_closure_messages: List[Message] = []
        for record in dispatch_records:
            dispatch_id = str(record.get("dispatch_id") or "").strip()
            dispatch_item_id = str(record.get("dispatch_item_id") or "").strip()
            item_id = str(record.get("acted_on_item_id") or "").strip()
            if not dispatch_id or not dispatch_item_id or not item_id:
                raise ValueError(f"[{self.name}] dispatch record missing required ids: {record!r}")

            source_meta = item_meta_by_id.get(item_id, {})
            source_summary = str(record.get("task_summary") or source_meta.get("summary") or item_id).strip() or item_id
            log_summary = f"Completed: {source_summary}"

            plan_id = str(record.get("plan_id") or source_meta.get("plan_id") or "").strip()
            task_id = str(record.get("task_id") or source_meta.get("task_id") or source_meta.get("item_id") or item_id).strip()
            effective_action_type = str(record.get("action_type") or action_type).strip() or action_type

            result_item_id = f"action_result:{dispatch_id}"
            result_meta: Dict[str, Any] = {
                "item_id": result_item_id,
                "source_type": "action_result",
                "event_type": "result",
                "created_at": now_utc.isoformat(),
                "created_at_local": created_at_local,
                "summary": log_summary,
                "importance": "medium",
                "actionability": "context_only",
                "state": "artifact",
                "state_reason": "action_completed",
                "last_reviewed_at": now_utc.isoformat(),
                "dispatch_id": dispatch_id,
                "acted_on_item_id": item_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "action_type": effective_action_type,
                "outcome": outcome,
                "reasoning": reasoning,
                "created_ticket_id": created_ticket_id,
            }
            result_messages.append(
                Message(
                    id=result_item_id,
                    data_type="dayflow_input_item",
                    sub_data_type=["dayflow_orchestrator", "action_log", "dayflow_action_log"],
                    sender="dayflow_orchestrator::action_selector",
                    content=result_meta["summary"],
                    timestamp=now_utc,
                    room_id=DAYFLOW_ROOM_ID,
                    metadata=result_meta,
                )
            )

            dispatch_close_meta: Dict[str, Any] = {
                "item_id": dispatch_item_id,
                "source_type": "action_dispatch",
                "event_type": "dispatch",
                "created_at": now_utc.isoformat(),
                "created_at_local": created_at_local,
                "summary": f"Dispatch completed for: {source_summary}",
                "importance": "medium",
                "actionability": "context_only",
                "state": "artifact",
                "state_reason": "action_completed",
                "last_reviewed_at": now_utc.isoformat(),
                "dispatch_id": dispatch_id,
                "dispatch_status": "completed",
                "completed_at": now_utc.isoformat(),
                "action_result_item_id": result_item_id,
                "acted_on_item_id": item_id,
                "task_id": task_id,
                "plan_id": plan_id,
                "action_type": effective_action_type,
            }
            dispatch_closure_messages.append(
                Message(
                    id=dispatch_item_id,
                    data_type="dayflow_input_item",
                    sub_data_type=["dayflow_orchestrator", "action_dispatch", "dayflow_action_log"],
                    sender="dayflow_orchestrator::action_selector",
                    content=dispatch_close_meta["summary"],
                    timestamp=now_utc,
                    room_id=DAYFLOW_ROOM_ID,
                    metadata=dispatch_close_meta,
                )
            )

        if result_messages:
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch as _write_batch
            all_msgs = result_messages + dispatch_closure_messages
            log_dicts = []
            for msg in all_msgs:
                meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else {}
                meta.setdefault("item_id", msg.id)
                meta.setdefault("summary", msg.content or "")
                meta.setdefault("source_type", meta.get("source_type", "action_log"))
                log_dicts.append(meta)
            _write_batch(log_dicts, caller=self.name)
            logger.info(
                "[%s] persisted %d action result(s) and closed %d dispatch marker(s).",
                self.name,
                len(result_messages),
                len(dispatch_closure_messages),
            )

    def _attach_execution_results(
        self,
        all_known_items: List[Dict[str, Any]],
        *,
        acted_on_ids: List[str],
        result_summary: str,
        now_utc: datetime,
    ) -> None:
        """Stamp execution_result onto plan steps that were just executed
        and close them via write_dayflow_item.
        """
        acted_set = {str(x or "").strip() for x in acted_on_ids if x}
        stamped = 0

        for item in all_known_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            if str(meta.get("source_type") or "").strip() not in {"plan", "plan_task"}:
                continue
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id not in acted_set:
                continue

            write_dayflow_item(
                item_id,
                state="closed",
                updates={
                    "execution_result": result_summary,
                    "executed_at": now_utc.isoformat(),
                    "planner_reviewed": False,
                },
                reason="dispatch_completed",
                caller=f"{self.name}::attach_execution_results",
            )
            meta.update({
                "execution_result": result_summary,
                "executed_at": now_utc.isoformat(),
                "planner_reviewed": False,
                "state": "closed",
                "state_reason": "dispatch_completed",
            })
            stamped += 1

        if stamped:
            logger.info(
                "[%s] stamped execution_result onto %d plan step(s) in-place.",
                self.name,
                stamped,
            )

    def _close_completed_plans(
        self,
        all_known_items: List[Dict[str, Any]],
        completed_plan_ids: set,
        now_utc: datetime,
    ) -> None:
        """Close all remaining tasks and mark synopses for completed plans."""
        from app.assistant.dayflow_orchestrator.state_store import RESOLVED_STATES
        _TERMINAL = RESOLVED_STATES
        closed_count = 0

        for item in all_known_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            plan_id = str(meta.get("plan_id") or "").strip()
            if not plan_id or plan_id not in completed_plan_ids:
                continue

            source_type = str(meta.get("source_type") or "").strip().lower()
            current_state = str(meta.get("state") or "").strip().lower()
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if not item_id:
                continue

            if source_type == "plan_synopsis":
                if current_state == "closed":
                    continue
                write_dayflow_item(
                    item_id,
                    updates={"plan_status": "completed", "completed_at": now_utc.isoformat()},
                    state="closed",
                    reason="plan_completed",
                    caller=f"{self.name}::close_completed_plans",
                )
                meta.update({"state": "closed", "plan_status": "completed"})
                closed_count += 1
            elif source_type in ("plan_task", "plan") and current_state not in _TERMINAL:
                write_dayflow_item(
                    item_id,
                    state="closed",
                    reason="plan_completed",
                    caller=f"{self.name}::close_completed_plans",
                )
                meta["state"] = "closed"
                closed_count += 1

        if closed_count:
            logger.info(
                "[%s] closed %d item(s) for completed plans: %s.",
                self.name, closed_count, completed_plan_ids,
            )

    def _cancel_tasks(
        self,
        all_known_items: List[Dict[str, Any]],
        closed_task_ids: set,
        now_utc: datetime,
    ) -> None:
        """Cancel individual tasks by ID."""
        from app.assistant.dayflow_orchestrator.state_store import RESOLVED_STATES
        _TERMINAL = RESOLVED_STATES
        to_persist: List[Message] = []

        for item in all_known_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            short_id = str(meta.get("short_id") or "").strip()
            if not item_id:
                continue
            if item_id not in closed_task_ids and short_id not in closed_task_ids:
                continue
            current_state = str(meta.get("state") or "").strip().lower()
            if current_state in _TERMINAL:
                continue

            write_dayflow_item(
                item_id,
                state="closed",
                reason="planner_cancelled",
                caller=f"{self.name}::cancel_tasks",
            )
            meta["state"] = "closed"
            to_persist.append(item_id)

        if to_persist:
            logger.info(
                "[%s] cancelled %d task(s): %s.",
                self.name,
                len(to_persist),
                closed_task_ids,
            )

