from __future__ import annotations

import os
import threading
import time
import uuid
import re
import json
from collections import deque
from concurrent.futures import Future, wait, FIRST_COMPLETED
from typing import Any, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext, ToolResult

from app.assistant.orchestrator_classes.types import ChildBudget, FactsState, SpawnSpec, TeamResult
from app.assistant.runtime import MonitoredThreadPoolExecutor

logger = get_logger(__name__)


class Orchestrator:
    """
    Base orchestrator runtime.

    - Owns a local blackboard (isolated from other managers/orchestrators).
    - Uses three LLM agents (by name) to decide:
        1) facts update + done decision (curator)
        2) routing decisions (router)
        3) spawn plan (architect)
    - Spawns child managers and/or sub-orchestrators in parallel using threads
      (bounded by max_children_concurrent). Cancellation is cooperative.
    """

    def __init__(self, name: str, orchestrator_config: dict, tool_registry, agent_registry):
        self.name = name
        self.config = orchestrator_config or {}
        self.tool_registry = tool_registry
        self.agent_registry = agent_registry

        self.blackboard = Blackboard()

        # Policy
        self.max_depth = int(self.config.get("max_depth", 2))
        self.max_children_total = int(self.config.get("max_children_total", 6))
        # Concurrency: children spawned in parallel by the orchestrator.
        # Default bumped to 24 (target range 20-30). Override via env:
        #   EMI_ORCHESTRATOR_MAX_CHILDREN_CONCURRENT=20..30
        default_children_concurrent = int(os.environ.get("EMI_ORCHESTRATOR_MAX_CHILDREN_CONCURRENT", "24"))
        self.max_children_concurrent = int(self.config.get("max_children_concurrent", default_children_concurrent))
        self.default_child_budget = self._parse_budget(self.config.get("default_child_budget") or {})

        # The architect produces the DAG / job plan.
        self.architect_agent_name = str(self.config.get("architect_agent") or "")
        self.facts_curator_agent_name = str(self.config.get("facts_curator_agent") or "")
        self.router_agent_name = str(self.config.get("router_agent") or "shared::orchestrator_router")
        # Optional finalizer (defaults to orchestrator-specific lightweight finalizer).
        self.final_answer_agent_name = str(self.config.get("final_answer_agent") or "shared::orchestrator_final_answer")

        self.busy = False
        self._cancel_event = threading.Event()

        self._facts = FactsState(version=0, facts={})
        self._results: list[TeamResult] = []

        # Job tracking (dependencies + de-dupe)
        self._job_status: dict[str, str] = {}   # job_id -> status
        self._job_child_id: dict[str, str] = {} # job_id -> child_id (running or last)
        self._pending_jobs: dict[str, SpawnSpec] = {}  # job_id -> latest spec (for dependency gating)
        self._terminal_job_ids: set[str] = set()
        self._result_index_by_job_id: dict[str, int] = {}

        # Brain agents are created lazily on first run, on this orchestrator blackboard.
        self._architect_agent = None
        self._facts_curator_agent = None
        self._router_agent = None
        self._final_answer_agent = None

        self._executor: Optional[MonitoredThreadPoolExecutor] = None
        # child_id -> {"spec": SpawnSpec, "future": Future, "instance": object|None, "started_at": float, "timed_out": bool}
        self._running: dict[str, dict[str, Any]] = {}
        self._spawned_total = 0

        # Orchestrator progress inbox (from event_hub)
        self._progress_lock = threading.Lock()
        self._progress_inbox = deque()  # items are dict payloads
        self._last_progress_received_at: float | None = None
        self._last_curator_run_at: float = 0.0
        self._progress_since_last_curator: int = 0
        self._progress_threshold: int = int(self.config.get("progress_threshold", 3) or 3)
        self._progress_token: str | None = None

    def _log_debug_exception(self, event: str, exc: BaseException, **context: Any) -> None:
        if isinstance(context, dict) and context:
            ctx = ", ".join(f"{k}={context[k]!r}" for k in sorted(context.keys()))
            logger.debug(
                "[orchestrator=%s] event=%s context={%s} error=%s",
                self.name,
                event,
                ctx,
                exc,
                exc_info=True,
            )
            return
        logger.debug(
            "[orchestrator=%s] event=%s error=%s",
            self.name,
            event,
            exc,
            exc_info=True,
        )

    def _on_orchestrator_progress(self, msg: Message) -> None:
        """
        EventHub handler: buffer progress items from running child managers.
        Expected shape:
          msg.event_topic == "orchestrator_progress"
          msg.receiver == this orchestrator name (best-effort)
          msg.data["job_id"], msg.data["progress_items"]
        """
        try:
            if msg is None:
                return
            # Optional receiver check: if provided, it must match this orchestrator.
            if isinstance(msg.receiver, str) and msg.receiver.strip() and (msg.receiver or "") != self.name:
                return
            data = msg.data or {}
            if not isinstance(data, dict):
                return
            items = data.get("progress_items")
            if not isinstance(items, list) or not items:
                return
            payload = {
                "job_id": data.get("job_id"),
                "agent": data.get("agent"),
                "manager_name": data.get("manager_name"),
                "major_impact": bool(data.get("major_impact", False)),
                "progress_items": items,
                "timestamp": getattr(msg, "timestamp", None),
            }
            with self._progress_lock:
                self._progress_inbox.append(payload)
                self._last_progress_received_at = time.time()
                self._progress_since_last_curator += len(items)
        except Exception as e:
            self._log_debug_exception("progress_message_ignored", e)
            return

    def is_busy(self) -> bool:
        return bool(self.busy)

    def cancel(self) -> None:
        """Cooperative cancel: stop spawning new children and stop the run loop."""
        self._cancel_event.set()
        # Best-effort cascade to running children.
        for meta in list(self._running.values()):
            if not isinstance(meta, dict):
                continue
            spec = meta.get("spec")
            inst = meta.get("instance")
            if not isinstance(spec, SpawnSpec):
                continue
            if spec.job_id in self._terminal_job_ids:
                continue
            self._terminal_job_ids.add(spec.job_id)
            self._job_status[spec.job_id] = "cancelled"
            # Mark child record so finish collection won't double-append.
            meta["cancelled"] = True
            try:
                self._results.append(
                    TeamResult(
                        job_id=spec.job_id,
                        child_id=str(meta.get("child_id") or self._job_child_id.get(spec.job_id) or ""),
                        child_kind=spec.child_kind,
                        child_type=spec.child_type,
                        status="cancelled",
                        summary="Cancelled (cooperative cancel requested).",
                    )
                )
                self._result_index_by_job_id.setdefault(spec.job_id, len(self._results) - 1)
            except Exception as e:
                self._log_debug_exception("cancel_append_result_failed", e, job_id=spec.job_id)
            self._request_cancel_instance(inst)

    def _request_cancel_running_children(self, *, targets: list[str] | None = None, reason: str) -> None:
        """
        Best-effort cooperative cancellation of currently-running children.
        Does NOT stop the orchestrator run loop by itself.
        """
        target_set = {t.strip() for t in (targets or []) if isinstance(t, str) and t.strip()}
        for child_id, meta in list(self._running.items()):
            if not isinstance(meta, dict):
                continue
            spec = meta.get("spec")
            inst = meta.get("instance")
            if not isinstance(spec, SpawnSpec):
                continue
            if spec.job_id in self._terminal_job_ids:
                continue

            if target_set:
                # Allow targeting by job_id or child_id substring match.
                if spec.job_id not in target_set and not any(t in str(child_id) for t in target_set):
                    continue

            self._terminal_job_ids.add(spec.job_id)
            self._job_status[spec.job_id] = "cancelled"
            meta["cancelled"] = True
            # Append a single terminal result if not already present.
            if spec.job_id not in self._result_index_by_job_id:
                try:
                    self._results.append(
                        TeamResult(
                            job_id=spec.job_id,
                            child_id=str(meta.get("child_id") or self._job_child_id.get(spec.job_id) or child_id),
                            child_kind=spec.child_kind,
                            child_type=spec.child_type,
                            status="cancelled",
                            summary=str(reason or "Cancelled (cooperative cancel requested).")[:1000],
                        )
                    )
                    self._result_index_by_job_id.setdefault(spec.job_id, len(self._results) - 1)
                except Exception as e:
                    self._log_debug_exception("cancel_targeted_append_result_failed", e, job_id=spec.job_id)
            self._request_cancel_instance(inst)

    def request_handler(self, user_message: Message, *, depth: int = 0) -> ToolResult:
        """
        Entry point similar to MultiAgentManager.request_handler.

        This is a synchronous run loop (create-only factory returns instance; caller chooses when to run).
        """
        self.busy = True
        try:
            self.blackboard.reset_blackboard()
            scope_payload = getattr(user_message, "scope_context", None)
            if isinstance(scope_payload, ScopeContext):
                self.blackboard.update_state_value("scope_context", scope_payload.model_dump())
            elif isinstance(scope_payload, dict):
                self.blackboard.update_state_value("scope_context", scope_payload)
            self.blackboard.update_state_value("task", user_message.task or user_message.content or "")
            self.blackboard.update_state_value("information", user_message.information or "")
            self.blackboard.update_state_value("orchestrator_depth", int(depth))
            # Orchestrator -> child shared context (planners can inject these keys easily).
            self.blackboard.update_state_value("orchestrator_global_objective", user_message.task or user_message.content or "")
            self.blackboard.update_state_value("orchestrator_global_guidance", self.config.get("global_guidance") or [])
            if isinstance(user_message.data, dict):
                for key, value in user_message.data.items():
                    self.blackboard.update_state_value(key, value)

            self._facts = FactsState(version=0, facts={})
            self._results = []
            self._running = {}
            self._spawned_total = 0
            self._job_status = {}
            self._job_child_id = {}
            self._pending_jobs = {}
            self._terminal_job_ids = set()
            self._result_index_by_job_id = {}
            self._cancel_event.clear()

            return self.run_loop(depth=depth)
        finally:
            self.busy = False

    def run(
        self,
        task: str,
        *,
        information: str = "",
        depth: int = 0,
        data: dict[str, Any] | None = None,
        sender: str = "User",
    ) -> ToolResult:
        """
        Convenience wrapper for smoke tests / scripting.

        Equivalent to calling request_handler(Message(...)).
        """
        return self.request_handler(
            Message(
                data_type="agent_activation",
                sender=sender,
                receiver=None,
                content="",
                task=str(task or ""),
                information=str(information or ""),
                data=data or {},
            ),
            depth=depth,
        )

    def _seconds_until_next_timeout_deadline(self) -> float | None:
        """
        Returns seconds until the soonest timeout deadline for any running child.
        None if no running children have a timeout configured.
        """
        now = time.time()
        soonest: float | None = None
        for meta in list(self._running.values()):
            if not isinstance(meta, dict):
                continue
            if meta.get("timed_out"):
                continue
            spec = meta.get("spec")
            started_at = meta.get("started_at")
            if not isinstance(spec, SpawnSpec) or not isinstance(started_at, (int, float)):
                continue
            timeout_s = int(getattr(spec.budget, "timeout_seconds", 0) or 0)
            if timeout_s <= 0:
                continue
            deadline = float(started_at) + float(timeout_s)
            if soonest is None or deadline < soonest:
                soonest = deadline
        if soonest is None:
            return None
        return max(0.0, soonest - now)

    def run_loop(self, *, depth: int = 0) -> ToolResult:
        if depth >= self.max_depth:
            return ToolResult(
                result_type="error",
                content=f"[{self.name}] Orchestrator depth limit reached (depth={depth}, max_depth={self.max_depth})",
                data={"orchestrator": self.name, "depth": depth, "max_depth": self.max_depth},
            )

        if not (self.architect_agent_name and self.facts_curator_agent_name and self.router_agent_name):
            return ToolResult(
                result_type="error",
                content=f"[{self.name}] Orchestrator missing required agent bindings (architect/facts_curator/router).",
                data={
                    "orchestrator": self.name,
                    "architect_agent": self.architect_agent_name,
                    "facts_curator_agent": self.facts_curator_agent_name,
                    "router_agent": self.router_agent_name,
                    "config": self.config,
                },
            )

        # Instantiate brain agents on this orchestrator's blackboard (lazy, reusable).
        architect, facts_curator, router = self._ensure_brain_agents()

        if not architect or not facts_curator or not router:
            return ToolResult(
                result_type="error",
                content=f"[{self.name}] Failed to instantiate orchestrator brain agents.",
                data={
                    "architect": bool(architect),
                    "facts_curator": bool(facts_curator),
                    "router": bool(router),
                },
            )

        self._executor = MonitoredThreadPoolExecutor(
            name=f"orchestrator-{self.name}",
            owner=self.name,
            max_workers=max(1, self.max_children_concurrent),
            metadata={"component": "orchestrator", "orchestrator_name": self.name},
        )

        tick = 0
        try:
            # --------------------------
            # Init phase (one-shot)
            # --------------------------
            # Take task from blackboard (already set in request_handler), run the architect ONCE,
            # validate/normalize spawn specs, and schedule eligible children.
            self.blackboard.update_state_value("orchestrator_phase", "init")
            self.blackboard.update_state_value("orchestrator_tick", tick)
            self.blackboard.update_state_value("orchestrator_facts_version", self._facts.version)
            self.blackboard.update_state_value("orchestrator_facts", self._facts.facts)
            self.blackboard.update_state_value("orchestrator_results", [])
            self.blackboard.update_state_value("orchestrator_job_status", dict(self._job_status))
            self.blackboard.update_state_value("orchestrator_running_jobs", [])
            self.blackboard.update_state_value("orchestrator_progress_inbox", [])
            self.blackboard.update_state_value("orchestrator_broadcast_history", [])
            self.blackboard.update_state_value("orchestrator_job_map", {})

            # Subscribe to progress events from child managers.
            try:
                if not isinstance(self._progress_token, str) or not self._progress_token.strip():
                    self._progress_token = f"orchestrator_progress::{self.name}::{uuid.uuid4().hex}"
                DI.event_hub.register_event(self._progress_token, self._on_orchestrator_progress)
                self.blackboard.update_state_value("orchestrator_progress_token", self._progress_token)
            except Exception as e:
                self._log_debug_exception("progress_handler_register_failed", e)

            # Publish allowlists/catalog so the architect cannot hallucinate manager types.
            allowed_mgrs = self.config.get("allowed_child_managers") or []
            allowed_orchs = self.config.get("allowed_child_orchestrators") or []
            if not isinstance(allowed_mgrs, list):
                allowed_mgrs = []
            if not isinstance(allowed_orchs, list):
                allowed_orchs = []
            allowed_mgrs = [str(x) for x in allowed_mgrs if str(x).strip()]
            allowed_orchs = [str(x) for x in allowed_orchs if str(x).strip()]
            self.blackboard.update_state_value("orchestrator_allowed_child_managers", allowed_mgrs)
            self.blackboard.update_state_value("orchestrator_allowed_child_orchestrators", allowed_orchs)
            mgr_catalog: dict[str, str] = {}
            try:
                for name in allowed_mgrs:
                    cfg = DI.manager_registry.get(name) if hasattr(DI, "manager_registry") else None
                    desc = ""
                    if isinstance(cfg, dict):
                        desc = str(cfg.get("description") or "")
                    mgr_catalog[name] = desc.strip()
            except Exception as e:
                self._log_debug_exception("allowed_manager_catalog_build_failed", e)
            self.blackboard.update_state_value("orchestrator_allowed_manager_catalog", mgr_catalog)

            init_arch_msg = Message(data_type="orchestrator_architecture_plan")
            # Capture the EXACT architect prompt on first architect run.
            try:
                already = bool(self.blackboard.get_state_value("orchestrator_architect_prompt_captured", False))
                if not already:
                    prompt_msgs = architect.construct_prompt(init_arch_msg)
                    self.blackboard.update_state_value("orchestrator_architect_prompt_messages", prompt_msgs)
                    self.blackboard.update_state_value("orchestrator_architect_prompt_captured", True)
                    if os.environ.get("EMI_PRINT_ARCHITECT_PROMPT", "0") == "1":
                        for m in prompt_msgs:
                            role = m.get("role")
                            content = m.get("content")
                            logger.debug(f"ARCHITECT PROMPT [{role}]: {content}")
            except Exception as e:
                self._log_debug_exception("architect_prompt_capture_failed", e)

            if os.environ.get("EMI_PRINT_ORCH_SEQUENCE", "0") == "1":
                logger.debug(f"[orchestrator:{self.name}] tick={tick} step=architect_init")
            architect.action_handler(init_arch_msg)
            init_arch_out = {
                "spawn": self.blackboard.get_state_value("spawn", []) or [],
                "notes": self.blackboard.get_state_value("notes", "") or "",
            }
            init_specs = self._normalize_spawn_specs(init_arch_out)
            for spec in init_specs:
                self._pending_jobs[spec.job_id] = spec

            before_running = len(self._running)
            self._schedule_children(list(self._pending_jobs.values()), depth=depth)
            scheduled_changed = len(self._running) != before_running

            if not scheduled_changed and not self._running:
                # If architect produced nothing (or all jobs failed to schedule), we cannot proceed.
                if not self._pending_jobs:
                    # If the architect did output jobs but scheduling failed (e.g., manager registry not loaded),
                    # surface that as a scheduling error instead of "no jobs".
                    if init_specs:
                        return ToolResult(
                            result_type="error",
                            content=f"[{self.name}] Failed to schedule any children during init.",
                            data={
                                "orchestrator": self.name,
                                "task": self.blackboard.get_state_value("task", ""),
                                "architect_spawn_count": len(init_specs),
                                "job_status": dict(self._job_status),
                                "results": [r.__dict__ for r in self._results],
                            },
                        )
                    return ToolResult(
                        result_type="error",
                        content=f"[{self.name}] Architect produced no jobs; nothing to run.",
                        data={"orchestrator": self.name, "task": self.blackboard.get_state_value("task", "")},
                    )
                # If there are pending jobs but none are eligible/allowed, surface blockers.
                any_eligible = any(
                    self._is_job_eligible(s) and self._is_allowed_spawn(s) for s in self._pending_jobs.values()
                )
                if not any_eligible:
                    blockers: dict[str, Any] = {}
                    for job_id, spec in self._pending_jobs.items():
                        deps = list(spec.depends_on or ())
                        blockers[job_id] = {
                            "depends_on": deps,
                            "dep_status": {d: self._job_status.get(d) for d in deps},
                            "job_status": self._job_status.get(job_id),
                            "allowed": self._is_allowed_spawn(spec),
                        }
                    return ToolResult(
                        result_type="error",
                        content=f"[{self.name}] Deadlock during init: no eligible pending jobs and nothing running.",
                        data={
                            "orchestrator": self.name,
                            "pending_jobs": list(self._pending_jobs.keys()),
                            "job_status": dict(self._job_status),
                            "blockers": blockers,
                        },
                    )

            self.blackboard.update_state_value("orchestrator_phase", "run")

            while not self._cancel_event.is_set():
                tick += 1
                self.blackboard.update_state_value("orchestrator_tick", tick)
                self.blackboard.update_state_value("orchestrator_facts_version", self._facts.version)
                self.blackboard.update_state_value("orchestrator_facts", self._facts.facts)
                # Publish allowlists/catalog so re-plans cannot hallucinate manager types.
                self.blackboard.update_state_value("orchestrator_allowed_child_managers", allowed_mgrs)
                self.blackboard.update_state_value("orchestrator_allowed_child_orchestrators", allowed_orchs)
                self.blackboard.update_state_value("orchestrator_allowed_manager_catalog", mgr_catalog)

                # 1) Collect finished children (non-blocking)
                before_results_len = len(self._results)
                self._collect_finished_children()
                finished_changed = len(self._results) != before_results_len

                before_timeouts_len = len(self._results)
                self._apply_timeouts()
                timeout_changed = len(self._results) != before_timeouts_len

                # Publish job state AFTER collecting finishes/timeouts so curator/router see consistent status.
                self._publish_job_state()

                # Drain buffered progress events (if any) and decide whether curator should run.
                progress_batch = []
                progress_major = False
                with self._progress_lock:
                    while self._progress_inbox:
                        item = self._progress_inbox.popleft()
                        if isinstance(item, dict):
                            progress_batch.append(item)
                            if bool(item.get("major_impact", False)):
                                progress_major = True
                if progress_batch:
                    # Keep a bounded tail for curator consumption.
                    existing = self.blackboard.get_state_value("orchestrator_progress_inbox", []) or []
                    if not isinstance(existing, list):
                        existing = []
                    merged = (existing + progress_batch)[-200:]
                    self.blackboard.update_state_value("orchestrator_progress_inbox", merged)

                now = time.time()
                should_run_curator = False
                if progress_major:
                    should_run_curator = True
                else:
                    # Hybrid batching policy:
                    # - wait >=10s since last curator
                    # - if threshold not met, wait up to 10s more
                    # - at 20s, run if any progress arrived
                    elapsed = now - float(self._last_curator_run_at or 0.0)
                    if elapsed >= 10.0:
                        if self._progress_since_last_curator >= self._progress_threshold:
                            should_run_curator = True
                        elif elapsed >= 20.0 and self._progress_since_last_curator > 0:
                            should_run_curator = True

                changed = bool(finished_changed or timeout_changed or should_run_curator)

                # Only run brain agents when something changed (child completion/timeout/progress threshold).
                if changed:
                    # Publish updated results/status.
                    self.blackboard.update_state_value(
                        "orchestrator_results",
                        [r.__dict__ for r in self._results[-20:]],
                    )
                    self.blackboard.update_state_value("orchestrator_job_status", dict(self._job_status))

                    # 2) Facts update + done decision
                    if os.environ.get("EMI_PRINT_ORCH_SEQUENCE", "0") == "1":
                        logger.debug(f"[orchestrator:{self.name}] tick={tick} step=facts_curator")
                    facts_curator.action_handler(Message(data_type="orchestrator_facts_curator"))
                    self._last_curator_run_at = time.time()
                    self._progress_since_last_curator = 0
                    facts_out = {
                        "is_done": bool(self.blackboard.get_state_value("is_done", False)),
                        "done_reason": str(self.blackboard.get_state_value("done_reason", "") or "").strip(),
                        "missing_requirements": self.blackboard.get_state_value("missing_requirements", []) or [],
                        "facts_patch": self.blackboard.get_state_value("facts_patch", {}) or {},
                        "notes": self.blackboard.get_state_value("notes", "") or "",
                    }
                    self._apply_facts_update(facts_out)

                    # IMPORTANT: publish the updated facts snapshot after applying the patch,
                    # so downstream brain agents (done_checker/architect/finalizer) see the latest state.
                    self.blackboard.update_state_value("orchestrator_facts_version", self._facts.version)
                    self.blackboard.update_state_value("orchestrator_facts", self._facts.facts)

                    # 3) Router (broadcast/cancel/replan decisions)
                    if os.environ.get("EMI_PRINT_ORCH_SEQUENCE", "0") == "1":
                        logger.debug(f"[orchestrator:{self.name}] tick={tick} step=router")
                    router.action_handler(Message(data_type="orchestrator_router"))
                    router_out = {
                        "replan_needed": bool(self.blackboard.get_state_value("replan_needed", False)),
                        "cancel_running": bool(self.blackboard.get_state_value("cancel_running", False)),
                        "cancel_targets": self.blackboard.get_state_value("cancel_targets", []) or [],
                        "broadcast": self.blackboard.get_state_value("broadcast", []) or [],
                        "notes": self.blackboard.get_state_value("notes", "") or "",
                    }

                    # Apply router outcomes: broadcast + cancellation.
                    try:
                        broadcast = router_out.get("broadcast")
                        if isinstance(broadcast, list):
                            self._apply_router_broadcast(broadcast)
                    except Exception as e:
                        self._log_debug_exception("router_broadcast_apply_failed", e)
                    try:
                        if router_out.get("cancel_running"):
                            cancel_targets = router_out.get("cancel_targets")
                            targets = cancel_targets if isinstance(cancel_targets, list) else None
                            self._request_cancel_running_children(
                                targets=targets,
                                reason="Cancelled due to router decision (moot/blocker/advance).",
                            )
                    except Exception as e:
                        self._log_debug_exception("router_cancel_apply_failed", e)

                    is_done = bool(facts_out.get("is_done", False))
                    done_reason = str(facts_out.get("done_reason", "") or "").strip()
                    missing_requirements = facts_out.get("missing_requirements", []) or []
                    done_out = {"is_done": is_done, "done_reason": done_reason, "missing_requirements": missing_requirements}

                    if is_done:
                        # If we are done, request cooperative cancellation for any still-running work.
                        if self._running:
                            self._request_cancel_running_children(
                                targets=None,
                                reason="Cancelled: orchestrator is done.",
                            )
                        # Produce a user-facing final answer using the configured finalizer agent.
                        self._set_final_answer_context(done_reason=done_reason)
                        finalizer = self._ensure_final_answer_agent()
                        try:
                            if finalizer is not None:
                                finalizer.action_handler(Message(data_type="orchestrator_final_answer"))
                            final_payload = self.blackboard.get_state_value("final_answer", None)
                            if isinstance(final_payload, dict):
                                answer_text = str(final_payload.get("final_answer_answer") or done_reason or "done").strip()
                                return ToolResult(
                                    result_type="final_answer",
                                    content=answer_text,
                                    data={
                                        "orchestrator": self.name,
                                        "facts": self._facts.facts,
                                        "facts_version": self._facts.version,
                                        "results": [r.__dict__ for r in self._results],
                                        "done": done_out,
                                        "final_answer": final_payload,
                                    },
                                )
                        except Exception as e:
                            self._log_debug_exception("finalizer_path_failed", e)
                        # Fallback: return done_reason if finalizer fails.
                        summary = done_reason or "done"
                        return ToolResult(
                            result_type="final_answer",
                            content=summary,
                            data={
                                "orchestrator": self.name,
                                "facts": self._facts.facts,
                                "facts_version": self._facts.version,
                                "results": [r.__dict__ for r in self._results],
                                "done": done_out,
                            },
                        )

                    # 4) Architecture plan (DAG/job recommendations)
                    # Only re-architect when a major-impact fact requires replanning
                    # OR if all work has stopped and we're not done (recovery).
                    replan_needed = bool(router_out.get("replan_needed", False))
                    if replan_needed or (not self._pending_jobs and not self._running):
                        # Publish missing requirements for the architect to consider (optional signal).
                        self.blackboard.update_state_value("orchestrator_missing_requirements", list(missing_requirements))

                        arch_msg = Message(data_type="orchestrator_architecture_plan")

                        if os.environ.get("EMI_PRINT_ORCH_SEQUENCE", "0") == "1":
                            logger.debug(f"[orchestrator:{self.name}] tick={tick} step=architect")
                        architect.action_handler(arch_msg)
                        arch_out = {
                            "spawn": self.blackboard.get_state_value("spawn", []) or [],
                            "notes": self.blackboard.get_state_value("notes", "") or "",
                        }
                        spawn_specs = self._normalize_spawn_specs(arch_out)
                    else:
                        spawn_specs = []

                    for spec in spawn_specs:
                        self._pending_jobs[spec.job_id] = spec

                    # Enforce total spawn limit
                    if self._spawned_total >= self.max_children_total and not self._running:
                        return ToolResult(
                            result_type="final_answer",
                            content=f"[{self.name}] Spawn limit reached; returning best-effort results.",
                            data={
                                "orchestrator": self.name,
                                "facts": self._facts.facts,
                                "facts_version": self._facts.version,
                                "results": [r.__dict__ for r in self._results],
                            },
                        )

                    # Try to schedule from pending; if we scheduled something, that's a "change".
                    before_running = len(self._running)
                    self._schedule_children(list(self._pending_jobs.values()), depth=depth)
                    scheduled_changed = len(self._running) != before_running

                    # Deadlock detection (only meaningful after a planning pass).
                    if not self._running and self._pending_jobs and not is_done:
                        any_eligible = any(self._is_job_eligible(s) for s in self._pending_jobs.values())
                        if not any_eligible:
                            blockers: dict[str, Any] = {}
                            for job_id, spec in self._pending_jobs.items():
                                deps = list(spec.depends_on or ())
                                blockers[job_id] = {
                                    "depends_on": deps,
                                    "dep_status": {d: self._job_status.get(d) for d in deps},
                                    "job_status": self._job_status.get(job_id),
                                }
                            return ToolResult(
                                result_type="error",
                                content=f"[{self.name}] Deadlock: no eligible pending jobs and nothing running.",
                                data={
                                    "orchestrator": self.name,
                                    "pending_jobs": list(self._pending_jobs.keys()),
                                    "job_status": dict(self._job_status),
                                    "blockers": blockers,
                                },
                            )

                    # If we just scheduled children, do NOT immediately re-run brain; wait for completion/timeout.
                    if scheduled_changed:
                        pass

                # Wait strategy: block until any child finishes OR next timeout deadline.
                if self._running:
                    futures = [m["future"] for m in self._running.values() if isinstance(m.get("future"), Future)]
                    delta = self._seconds_until_next_timeout_deadline()
                    # Cap the wait so cancellation is responsive.
                    wait_timeout = 1.0 if delta is None else min(max(0.0, float(delta)), 1.0)
                    wait(futures, timeout=wait_timeout, return_when=FIRST_COMPLETED)
                else:
                    # Avoid busy loop when no work is scheduled.
                    time.sleep(0.1)

            return ToolResult(
                result_type="final_answer",
                content=f"[{self.name}] Cancelled.",
                data={"orchestrator": self.name, "cancelled": True},
            )
        finally:
            try:
                # Unsubscribe progress handler to avoid leaking subscriptions.
                try:
                    if isinstance(self._progress_token, str) and self._progress_token.strip():
                        DI.event_hub.unregister_event(self._progress_token, self._on_orchestrator_progress)
                except Exception as e:
                    self._log_debug_exception("progress_handler_unregister_failed", e)
                # Best-effort cascade cancel on exit.
                for meta in list(self._running.values()):
                    if not isinstance(meta, dict):
                        continue
                    inst = meta.get("instance")
                    self._request_cancel_instance(inst)
                if self._executor:
                    self._executor.shutdown(wait=False)
            except Exception as e:
                self._log_debug_exception("shutdown_cleanup_failed", e)
            self._executor = None

    # --------------------------
    # Internal helpers
    # --------------------------

    def _parse_budget(self, d: dict[str, Any]) -> ChildBudget:
        try:
            mc = int(d.get("max_cycles", 30))
        except Exception as e:
            self._log_debug_exception("budget_parse_max_cycles_failed", e)
            mc = 30
        try:
            ts = int(d.get("timeout_seconds", 180))
        except Exception as e:
            self._log_debug_exception("budget_parse_timeout_seconds_failed", e)
            ts = 180
        return ChildBudget(max_cycles=mc, timeout_seconds=ts)

    def _ensure_brain_agents(self):
        """
        Create (or reuse) the three brain agents bound to this orchestrator's blackboard.
        This makes testing easier (tests can stub llm_interface before running).
        """
        if self._architect_agent is None:
            self._architect_agent = DI.agent_factory.create_agent(self.architect_agent_name, self.blackboard)
        if self._facts_curator_agent is None:
            self._facts_curator_agent = DI.agent_factory.create_agent(self.facts_curator_agent_name, self.blackboard)
        if self._router_agent is None:
            self._router_agent = DI.agent_factory.create_agent(self.router_agent_name, self.blackboard)
        return self._architect_agent, self._facts_curator_agent, self._router_agent

    def _publish_job_state(self) -> None:
        """
        Publish job state to the blackboard for curator/router:
        - orchestrator_running_jobs: running child_id/type list (for deterministic targeting)
        - orchestrator_job_map: job_id -> contract/status summary
        """
        try:
            running_jobs = []
            job_map: dict[str, Any] = {}

            # Running jobs (from _running)
            for cid, meta in list(self._running.items()):
                if not isinstance(meta, dict):
                    continue
                spec = meta.get("spec")
                if not isinstance(spec, SpawnSpec):
                    continue
                started_at = meta.get("started_at")
                running_jobs.append(
                    {
                        "job_id": spec.job_id,
                        "child_kind": spec.child_kind,
                        "child_type": spec.child_type,
                        "child_id": str(cid),
                    }
                )
                job_map[str(spec.job_id)] = {
                    "job_id": spec.job_id,
                    "status": "running",
                    "child_kind": spec.child_kind,
                    "child_type": spec.child_type,
                    "child_id": str(cid),
                    "depends_on": list(spec.depends_on or []),
                    "task": spec.task,
                    "success_criteria": spec.success_criteria,
                    "information": spec.information,
                    "started_at": started_at,
                }

            # Pending + terminal jobs (from _pending_jobs + _job_status)
            for job_id, spec in list(self._pending_jobs.items()):
                if not isinstance(spec, SpawnSpec):
                    continue
                if str(job_id) in job_map:
                    continue
                job_map[str(job_id)] = {
                    "job_id": spec.job_id,
                    "status": str(self._job_status.get(spec.job_id, "pending")),
                    "child_kind": spec.child_kind,
                    "child_type": spec.child_type,
                    "child_id": self._job_child_id.get(spec.job_id),
                    "depends_on": list(spec.depends_on or []),
                    "task": spec.task,
                    "success_criteria": spec.success_criteria,
                    "information": spec.information,
                    "started_at": None,
                }

            # Terminal jobs not in pending map (best-effort: publish status)
            for job_id, st in list(self._job_status.items()):
                if str(job_id) in job_map:
                    continue
                if st not in {"success", "error", "timeout", "cancelled"}:
                    continue
                job_map[str(job_id)] = {"job_id": str(job_id), "status": str(st)}

            self.blackboard.update_state_value("orchestrator_running_jobs", running_jobs)
            self.blackboard.update_state_value("orchestrator_job_map", job_map)
        except Exception as e:
            self._log_debug_exception("publish_job_state_failed", e)
            return

    def _ensure_final_answer_agent(self):
        if self._final_answer_agent is None and self.final_answer_agent_name:
            self._final_answer_agent = DI.agent_factory.create_agent(self.final_answer_agent_name, self.blackboard)
        return self._final_answer_agent

    def _set_final_answer_context(self, *, done_reason: str = "") -> None:
        """
        Prepare blackboard fields expected by shared::final_answer (and similar) so a finalizer
        can generate a user-facing answer from orchestrator state.
        """
        try:
            bundle = {
                "done_reason": done_reason,
                "facts_version": self._facts.version,
                "facts": self._facts.facts,
                "results": [r.__dict__ for r in self._results],
            }
            txt = json.dumps(bundle, ensure_ascii=True)
        except Exception as e:
            self._log_debug_exception("final_answer_context_serialize_failed", e)
            txt = str({"done_reason": done_reason})
        self.blackboard.update_state_value("final_answer_content", txt)

    def _derive_job_id(self, *, kind: str, child_type: str, task: str) -> str:
        base = f"{kind}_{child_type}_{task}".lower()
        base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
        return base[:80] if base else f"job_{uuid.uuid4().hex[:8]}"

    def _normalize_spawn_specs(self, spawn_out: Any) -> list[SpawnSpec]:
        if not isinstance(spawn_out, dict):
            return []
        raw = spawn_out.get("spawn") or spawn_out.get("spawns") or []
        if not isinstance(raw, list):
            return []
        job_task_map = {}
        try:
            job_task_map = self.blackboard.get_state_value("orchestrator_job_task_map") or {}
        except Exception as e:
            self._log_debug_exception("job_task_map_read_failed", e)
            job_task_map = {}
        out: list[SpawnSpec] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            job_id = item.get("job_id")
            depends_on_raw = item.get("depends_on") or item.get("dependsOn") or []

            # Accept both internal SpawnSpec keys and the more human-friendly
            # orchestrator-architect output keys (manager_type/sub_task_for_manager).
            kind = item.get("child_kind")
            ctype = item.get("child_type")
            task = item.get("task")
            if kind not in ("manager", "orchestrator"):
                kind = None

            if kind is None:
                # Architect format: manager_type + task_description
                if isinstance(item.get("manager_type"), str) and item.get("manager_type").strip():
                    kind = "manager"
                    ctype = item.get("manager_type")
                elif isinstance(item.get("orchestrator_type"), str) and item.get("orchestrator_type").strip():
                    kind = "orchestrator"
                    ctype = item.get("orchestrator_type")

            if not isinstance(task, str) or not task.strip():
                td = item.get("sub_task_for_manager")
                if not (isinstance(td, str) and td.strip()):
                    # Back-compat for older architect outputs.
                    td = item.get("task_description")
                if isinstance(td, str) and td.strip():
                    task = td

            if kind not in ("manager", "orchestrator"):
                continue
            if not isinstance(ctype, str) or not ctype.strip():
                continue
            if not isinstance(task, str) or not task.strip():
                continue

            if not isinstance(job_id, str) or not job_id.strip():
                job_id = self._derive_job_id(kind=str(kind), child_type=ctype.strip(), task=task.strip())

            depends_on: list[str] = []
            if isinstance(depends_on_raw, list):
                for d in depends_on_raw:
                    if isinstance(d, str) and d.strip():
                        depends_on.append(d.strip())
            # Budget: if omitted, use orchestrator default_child_budget (not the dataclass defaults).
            budget_obj = self.default_child_budget
            if isinstance(item.get("budget"), dict):
                budget_d = item.get("budget") or {}
                try:
                    mc = int(budget_d.get("max_cycles", getattr(self.default_child_budget, "max_cycles", 30)))
                except Exception as e:
                    self._log_debug_exception("budget_override_max_cycles_failed", e, job_id=job_id)
                    mc = int(getattr(self.default_child_budget, "max_cycles", 30))
                try:
                    ts = int(budget_d.get("timeout_seconds", getattr(self.default_child_budget, "timeout_seconds", 180)))
                except Exception as e:
                    self._log_debug_exception("budget_override_timeout_failed", e, job_id=job_id)
                    ts = int(getattr(self.default_child_budget, "timeout_seconds", 180))
                budget_obj = ChildBudget(max_cycles=mc, timeout_seconds=ts)
            out.append(
                self._apply_job_task_map(
                    SpawnSpec(
                        job_id=str(job_id).strip(),
                        depends_on=tuple(depends_on),
                        child_kind=kind,
                        child_type=ctype.strip(),
                        task=task.strip(),
                        information=str(item.get("information") or ""),
                        inputs=item.get("inputs") if isinstance(item.get("inputs"), dict) else None,
                        budget=budget_obj,
                        success_criteria=str(item.get("success_criteria") or ""),
                    ),
                    job_task_map,
                )
            )
        return out

    def _apply_job_task_map(self, spec: SpawnSpec, job_task_map: dict) -> SpawnSpec:
        if not isinstance(job_task_map, dict) or not isinstance(spec, SpawnSpec):
            return spec
        override = job_task_map.get(spec.job_id)
        if not isinstance(override, dict):
            return spec
        task = override.get("task") or spec.task
        information = override.get("information") or spec.information
        inputs = override.get("inputs") if isinstance(override.get("inputs"), dict) else spec.inputs
        success_criteria = override.get("success_criteria") or spec.success_criteria
        depends_on = override.get("depends_on")
        if not isinstance(depends_on, list):
            depends_on = list(spec.depends_on or ())
        return SpawnSpec(
            job_id=spec.job_id,
            depends_on=tuple(depends_on),
            child_kind=spec.child_kind,
            child_type=spec.child_type,
            task=str(task or ""),
            information=str(information or ""),
            inputs=inputs,
            budget=spec.budget,
            success_criteria=str(success_criteria or ""),
        )

    def _schedule_children(self, specs: list[SpawnSpec], *, depth: int) -> None:
        if not specs:
            return
        if not self._executor:
            return

        # Fill available concurrency slots.
        available = max(0, self.max_children_concurrent - len(self._running))
        if available <= 0:
            return

        # Prefer independent jobs first (fewer deps).
        specs_sorted = sorted(specs, key=lambda s: len(getattr(s, "depends_on", ()) or ()))
        scheduled_now = 0
        for spec in specs_sorted:
            if scheduled_now >= available:
                break
            if self._spawned_total >= self.max_children_total:
                break
            # Dependency gating + de-dupe by job_id (MVP: deps must be success).
            if not self._is_job_eligible(spec):
                continue
            if not self._is_allowed_spawn(spec):
                self._results.append(
                    TeamResult(
                        job_id=spec.job_id,
                        child_id=f"rejected_{uuid.uuid4().hex[:8]}",
                        child_kind=spec.child_kind,
                        child_type=spec.child_type,
                        status="error",
                        summary=f"Rejected spawn by allowlist: {spec.child_kind}:{spec.child_type}",
                    )
                )
                self._job_status[spec.job_id] = "error"
                # Remove from pending if present.
                self._pending_jobs.pop(spec.job_id, None)
                continue
            child_id = f"{spec.child_kind}_{spec.child_type}_{uuid.uuid4().hex[:8]}"
            # Instantiate child in orchestrator thread so we can cooperatively cancel it later.
            inst = None
            try:
                if spec.child_kind == "manager":
                    inst = DI.multi_agent_manager_factory.create_manager(
                        spec.child_type,
                        name=f"{spec.child_type}__{child_id}",
                    )
                    try:
                        inst.manager_config["max_cycles"] = int(spec.budget.max_cycles)
                    except Exception as e:
                        self._log_debug_exception("child_max_cycles_override_apply_failed", e, child_type=spec.child_type)
                    # Optional test hook: allow passing LLM stubs in inputs.
                    self._maybe_apply_llm_stubs(inst, spec.inputs)
                else:
                    inst = DI.orchestrator_factory.create_orchestrator(
                        spec.child_type,
                        name=f"{spec.child_type}__{child_id}",
                    )
            except Exception as e:
                self._log_debug_exception("child_instance_create_failed", e, child_kind=spec.child_kind, child_type=spec.child_type)
                self._results.append(
                    TeamResult(
                        job_id=spec.job_id,
                        child_id=child_id,
                        child_kind=spec.child_kind,
                        child_type=spec.child_type,
                        status="error",
                        summary=f"Failed to create child instance: {e}",
                    )
                )
                self._job_status[spec.job_id] = "error"
                self._pending_jobs.pop(spec.job_id, None)
                continue

            fut = self._executor.submit(self._run_child_instance, child_id, spec, inst, depth)
            self._running[child_id] = {
                "child_id": child_id,
                "spec": spec,
                "future": fut,
                "instance": inst,
                "started_at": time.time(),
                "timed_out": False,
            }
            self._job_child_id[spec.job_id] = child_id
            self._job_status.setdefault(spec.job_id, "running")
            self._spawned_total += 1
            scheduled_now += 1
            # Remove from pending once scheduled.
            self._pending_jobs.pop(spec.job_id, None)

    def _is_job_eligible(self, spec: SpawnSpec) -> bool:
        # De-dupe: skip if already running or finished.
        st = self._job_status.get(spec.job_id)
        if st in {"running", "success", "error", "timeout", "cancelled"}:
            return False
        for dep in (spec.depends_on or ()):
            if self._job_status.get(dep) != "success":
                return False
        return True

    def _is_allowed_spawn(self, spec: SpawnSpec) -> bool:
        allowed_mgrs = self.config.get("allowed_child_managers")
        allowed_orchs = self.config.get("allowed_child_orchestrators")
        if spec.child_kind == "manager":
            if isinstance(allowed_mgrs, list) and allowed_mgrs:
                return spec.child_type in {str(x) for x in allowed_mgrs}
            return True
        if spec.child_kind == "orchestrator":
            if isinstance(allowed_orchs, list) and allowed_orchs:
                return spec.child_type in {str(x) for x in allowed_orchs}
            return True
        return False

    def _maybe_apply_llm_stubs(self, manager_inst: object, inputs: dict[str, Any] | None) -> None:
        """
        Test-only hook: if inputs contains __llm_stub, set llm_interface on the specified agents.
        Shape:
          inputs["__llm_stub"] = {
            "agent_name": {"outputs": [...], "sleep_s": 1.0}
          }
        """
        if manager_inst is None or not isinstance(inputs, dict):
            return
        stub = inputs.get("__llm_stub")
        if not isinstance(stub, dict) or not stub:
            return

        class _SeqLLM:
            def __init__(self, outputs, sleep_s: float = 0.0):
                self._outputs = list(outputs)
                self._sleep_s = float(sleep_s or 0.0)

            def structured_output(self, messages, use_json=False, **params):
                if self._sleep_s > 0:
                    time.sleep(self._sleep_s)
                if not self._outputs:
                    raise RuntimeError("No more stubbed LLM outputs available")
                return self._outputs.pop(0)

        try:
            reg = getattr(manager_inst, "agent_registry", None)
            getter = getattr(reg, "get_agent_instance", None) if reg is not None else None
            if not callable(getter):
                return
            for agent_name, cfg in stub.items():
                if not isinstance(agent_name, str) or not agent_name:
                    continue
                if isinstance(cfg, dict):
                    outputs = cfg.get("outputs")
                    sleep_s = cfg.get("sleep_s", 0.0)
                else:
                    outputs = cfg
                    sleep_s = 0.0
                if not isinstance(outputs, list):
                    continue
                agent = getter(agent_name)
                if agent is None:
                    continue
                agent.llm_interface = _SeqLLM(outputs=outputs, sleep_s=float(sleep_s or 0.0))
        except Exception as e:
            self._log_debug_exception("llm_stub_apply_failed", e)
            return

    def _collect_finished_children(self) -> None:
        done_ids: list[str] = []
        for child_id, meta in list(self._running.items()):
            spec = meta.get("spec")
            fut = meta.get("future")
            if not isinstance(spec, SpawnSpec) or not isinstance(fut, Future):
                done_ids.append(child_id)
                continue
            if not fut.done():
                continue
            done_ids.append(child_id)
            try:
                res = fut.result()
            except BaseException as e:
                self._log_debug_exception("child_future_exception", e, job_id=spec.job_id)
                # If job already terminal (timeout/cancelled), record late crash in raw only.
                if spec.job_id in self._terminal_job_ids:
                    idx = self._result_index_by_job_id.get(spec.job_id)
                    if isinstance(idx, int) and 0 <= idx < len(self._results):
                        try:
                            existing = self._results[idx]
                            raw = existing.raw if isinstance(existing.raw, dict) else {"raw": existing.raw}
                            raw["late_exception"] = str(e)
                            existing.raw = raw
                        except Exception as late_exc:
                            self._log_debug_exception("late_exception_metadata_attach_failed", late_exc, job_id=spec.job_id)
                else:
                    self._results.append(
                        TeamResult(
                            job_id=spec.job_id,
                            child_id=child_id,
                            child_kind=getattr(spec, "child_kind", "manager"),
                            child_type=getattr(spec, "child_type", ""),
                            status="error",
                            summary=f"Child crashed: {e}",
                        )
                    )
                    self._result_index_by_job_id.setdefault(spec.job_id, len(self._results) - 1)
                    self._job_status[spec.job_id] = "error"
                    self._terminal_job_ids.add(spec.job_id)
                continue
            if isinstance(res, TeamResult):
                if spec.job_id in self._terminal_job_ids:
                    idx = self._result_index_by_job_id.get(spec.job_id)
                    if isinstance(idx, int) and 0 <= idx < len(self._results):
                        try:
                            existing = self._results[idx]
                            raw = existing.raw if isinstance(existing.raw, dict) else {"raw": existing.raw}
                            raw["late_result"] = res.__dict__
                            existing.raw = raw
                        except Exception as late_exc:
                            self._log_debug_exception("late_result_metadata_attach_failed", late_exc, job_id=spec.job_id)
                else:
                    self._results.append(res)
                    self._result_index_by_job_id.setdefault(spec.job_id, len(self._results) - 1)
                    self._job_status[spec.job_id] = res.status
                    if res.status in {"success", "error", "timeout", "cancelled"}:
                        self._terminal_job_ids.add(spec.job_id)
            else:
                if spec.job_id in self._terminal_job_ids:
                    idx = self._result_index_by_job_id.get(spec.job_id)
                    if isinstance(idx, int) and 0 <= idx < len(self._results):
                        try:
                            existing = self._results[idx]
                            raw = existing.raw if isinstance(existing.raw, dict) else {"raw": existing.raw}
                            raw["late_result"] = {"status": "success", "summary": str(res)}
                            existing.raw = raw
                        except Exception as late_exc:
                            self._log_debug_exception("late_non_team_result_metadata_attach_failed", late_exc, job_id=spec.job_id)
                else:
                    self._results.append(
                        TeamResult(
                            job_id=spec.job_id,
                            child_id=child_id,
                            child_kind=getattr(spec, "child_kind", "manager"),
                            child_type=getattr(spec, "child_type", ""),
                            status="success",
                            summary=str(res),
                        )
                    )
                    self._result_index_by_job_id.setdefault(spec.job_id, len(self._results) - 1)
                    self._job_status[spec.job_id] = "success"
                    self._terminal_job_ids.add(spec.job_id)
        for cid in done_ids:
            self._running.pop(cid, None)

    def _apply_facts_update(self, facts_out: Any) -> None:
        if not isinstance(facts_out, dict):
            return
        patch = facts_out.get("facts_patch")
        # Accept either dict patch (legacy) or list-of-items patch (OpenAI schema-safe).
        if isinstance(patch, list):
            d: dict[str, Any] = {}
            for item in patch:
                if not isinstance(item, dict):
                    continue
                k = item.get("key")
                if not isinstance(k, str) or not k.strip():
                    continue
                d[k.strip()] = item.get("value")
            patch = d
        if not isinstance(patch, dict) or not patch:
            patch = {}
        # Shallow merge for MVP
        facts = dict(self._facts.facts or {})
        if patch:
            facts.update(patch)
            self._facts = FactsState(version=self._facts.version + 1, facts=facts)

    def _apply_router_broadcast(self, broadcast: list[dict]) -> None:
        """
        Apply router broadcast plan by forwarding to children and recording history.
        """
        if not isinstance(broadcast, list) or not broadcast:
            return
        try:
            tick = int(self.blackboard.get_state_value("orchestrator_tick", 0) or 0)
        except Exception as e:
            self._log_debug_exception("broadcast_tick_read_failed", e)
            tick = 0
        for item in broadcast:
            if not isinstance(item, dict):
                continue
            target = item.get("target")
            msg = item.get("message")
            if not isinstance(target, str) or not target.strip():
                continue
            if not isinstance(msg, str) or not msg.strip():
                continue
            self._broadcast_to_children(target.strip(), msg.strip())
            try:
                hist = self.blackboard.get_state_value("orchestrator_broadcast_history", []) or []
                if not isinstance(hist, list):
                    hist = []
                hist.append({"tick": tick, "ts": time.time(), "target": target.strip(), "message": msg.strip()})
                self.blackboard.update_state_value("orchestrator_broadcast_history", hist[-200:])
            except Exception as e:
                self._log_debug_exception("broadcast_history_append_failed", e, target=target)

    def _broadcast_to_children(self, target: str, message: str) -> None:
        for child_id, meta in list(self._running.items()):
            if not isinstance(meta, dict):
                continue
            spec = meta.get("spec")
            inst = meta.get("instance")
            if not isinstance(spec, SpawnSpec):
                continue
            # Explicit targeting only (avoid accidental broad broadcasts):
            # - job_id: exact match to spec.job_id
            # - type:<child_type>: broadcast to all running children of that type
            # - child_id:<substring>: match by substring in child_id
            matches = False
            try:
                if target == spec.job_id:
                    matches = True
                elif target.startswith("type:") and target[len("type:") :] == spec.child_type:
                    matches = True
                elif target.startswith("child_id:") and target[len("child_id:") :] in str(child_id):
                    matches = True
            except Exception as e:
                self._log_debug_exception("broadcast_target_match_failed", e, target=target, child_id=child_id)
                matches = False
            if not matches:
                continue
            try:
                bb = getattr(inst, "blackboard", None)
                if bb is None:
                    continue
                appender = getattr(bb, "append_global_state_value", None)
                if callable(appender):
                    appender("orchestrator_inbox", {"from": self.name, "target": target, "message": message})
                else:
                    # fallback: local append
                    appender2 = getattr(bb, "append_state_value", None)
                    if callable(appender2):
                        appender2("orchestrator_inbox", {"from": self.name, "target": target, "message": message})
            except Exception as e:
                self._log_debug_exception("broadcast_delivery_failed", e, target=target, child_id=child_id)
                continue

    def _run_child_instance(self, child_id: str, spec: SpawnSpec, inst: object, parent_depth: int) -> TeamResult:
        started = time.time()
        try:
            inherited_scope: ScopeContext | None = None
            try:
                inherited_scope_raw = self.blackboard.get_state_value("scope_context", None)
                if isinstance(inherited_scope_raw, ScopeContext):
                    inherited_scope = inherited_scope_raw
                elif isinstance(inherited_scope_raw, dict):
                    inherited_scope = ScopeContext.model_validate(inherited_scope_raw)
            except Exception as e:
                self._log_debug_exception("orchestrator_scope_inheritance_failed", e, child_id=child_id, job_id=spec.job_id)
                inherited_scope = None
            if inherited_scope is None:
                inherited_scope = build_system_scope_context(
                    owner_id="system",
                    actor_id=self.name,
                    surface="orchestrator",
                    scope_id=f"scope::orchestrator::{self.name}::{spec.job_id}",
                )

            # Merge orchestrator facts into child inputs (read-only snapshot).
            child_inputs = dict(spec.inputs or {})
            # Apply default_child_inputs from orchestrator config — lowest priority,
            # overridden by anything the architect explicitly sets in spec.inputs.
            default_child_inputs = self.config.get("default_child_inputs") or {}
            if isinstance(default_child_inputs, dict):
                for k, v in default_child_inputs.items():
                    child_inputs.setdefault(k, v)
            child_inputs.setdefault("orchestrator_facts_version", self._facts.version)
            child_inputs.setdefault("orchestrator_facts", dict(self._facts.facts or {}))
            # Provide a compact dependency bundle so downstream jobs (e.g., email managers) can use upstream results
            # without querying external stores (KG, memory, etc.).
            dep_bundle: dict[str, Any] = {}
            try:
                for dep_id in list(spec.depends_on or ()):
                    idx = self._result_index_by_job_id.get(dep_id)
                    if not isinstance(idx, int) or not (0 <= idx < len(self._results)):
                        continue
                    r = self._results[idx]
                    if not isinstance(r, TeamResult):
                        continue
                    dep_bundle[str(dep_id)] = {
                        "job_id": r.job_id,
                        "status": r.status,
                        "summary": str(r.summary or ""),
                        "raw": r.raw,
                    }
            except Exception as e:
                self._log_debug_exception("dependency_bundle_build_failed", e, child_id=child_id, job_id=spec.job_id)
                dep_bundle = {}
            if dep_bundle:
                child_inputs.setdefault("orchestrator_dependency_results", dep_bundle)
            # Orchestrator-level guidance/context (planner-visible).
            child_inputs.setdefault(
                "orchestrator_global_objective",
                str(self.blackboard.get_state_value("orchestrator_global_objective", "") or ""),
            )
            child_inputs.setdefault(
                "orchestrator_global_guidance",
                self.blackboard.get_state_value("orchestrator_global_guidance", []) or [],
            )
            child_inputs.setdefault("orchestrator_name", str(self.name))
            # Job-level guidance: prefer explicit spec.information.
            child_inputs.setdefault("orchestrator_job_id", str(spec.job_id))
            child_inputs.setdefault("orchestrator_job_guidance", str(spec.information or ""))
            if isinstance(self._progress_token, str) and self._progress_token.strip():
                child_inputs.setdefault("orchestrator_progress_token", self._progress_token)

            if spec.child_kind == "manager":
                info = str(spec.information or "")
                if dep_bundle:
                    try:
                        dep_txt = json.dumps(dep_bundle, ensure_ascii=True)
                    except Exception as e:
                        self._log_debug_exception(
                            "dependency_bundle_serialize_manager_failed",
                            e,
                            child_id=child_id,
                            job_id=spec.job_id,
                        )
                        dep_txt = str(dep_bundle)
                    info = (info + "\n\n" if info.strip() else "") + "Upstream dependency results:\n" + dep_txt
                msg = Message(
                    task=spec.task,
                    information=info,
                    data=child_inputs,
                    content=spec.task,
                    scope_context=inherited_scope,
                )
                out = DI.manager_invoker.invoke(inst, msg)
                return TeamResult(
                    job_id=spec.job_id,
                    child_id=child_id,
                    child_kind=spec.child_kind,
                    child_type=spec.child_type,
                    status="success" if getattr(out, "result_type", "") != "error" else "error",
                    summary=str(getattr(out, "content", "") or ""),
                    raw=getattr(out, "data", None),
                )

            # sub-orchestrator
            info = str(spec.information or "")
            if dep_bundle:
                try:
                    dep_txt = json.dumps(dep_bundle, ensure_ascii=True)
                except Exception as e:
                    self._log_debug_exception(
                        "dependency_bundle_serialize_orchestrator_failed",
                        e,
                        child_id=child_id,
                        job_id=spec.job_id,
                    )
                    dep_txt = str(dep_bundle)
                info = (info + "\n\n" if info.strip() else "") + "Upstream dependency results:\n" + dep_txt
            msg = Message(
                task=spec.task,
                information=info,
                data=child_inputs,
                content=spec.task,
                scope_context=inherited_scope,
            )
            out = inst.request_handler(msg, depth=parent_depth + 1)
            return TeamResult(
                job_id=spec.job_id,
                child_id=child_id,
                child_kind=spec.child_kind,
                child_type=spec.child_type,
                status="success" if getattr(out, "result_type", "") != "error" else "error",
                summary=str(getattr(out, "content", "") or ""),
                raw=getattr(out, "data", None),
            )
        except BaseException as e:
            elapsed = time.time() - started
            return TeamResult(
                job_id=spec.job_id,
                child_id=child_id,
                child_kind=spec.child_kind,
                child_type=spec.child_type,
                status="error",
                summary=f"Exception after {elapsed:.1f}s: {e}",
            )

    def _apply_timeouts(self) -> None:
        """
        Best-effort timeout handling under threads + cooperative cancel.
        If a child exceeds its timeout, we mark it timed_out and request cancellation.
        """
        now = time.time()
        for child_id, meta in list(self._running.items()):
            if not isinstance(meta, dict):
                continue
            if meta.get("timed_out"):
                continue
            spec = meta.get("spec")
            started_at = meta.get("started_at")
            inst = meta.get("instance")
            if not isinstance(spec, SpawnSpec) or not isinstance(started_at, (int, float)):
                continue
            timeout_s = int(getattr(spec.budget, "timeout_seconds", 0) or 0)
            if timeout_s <= 0:
                continue
            if (now - float(started_at)) < float(timeout_s):
                continue

            meta["timed_out"] = True
            self._request_cancel_instance(inst)
            # Best-effort: capture partial progress from the child blackboard so the curator
            # can see what was discovered before timeout.
            raw_snapshot: dict[str, Any] = {"timeout_seconds": timeout_s}
            try:
                bb = getattr(inst, "blackboard", None)
                getter = getattr(bb, "get_state_value", None) if bb is not None else None
                if callable(getter):
                    raw_snapshot["progress_report"] = getter("progress_report", None)
                    raw_snapshot["checklist"] = getter("checklist", None)
                    raw_snapshot["summary"] = getter("summary", None)
            except Exception as e:
                self._log_debug_exception("timeout_raw_snapshot_capture_failed", e, child_id=child_id, job_id=spec.job_id)
            self._results.append(
                TeamResult(
                    job_id=spec.job_id,
                    child_id=child_id,
                    child_kind=spec.child_kind,
                    child_type=spec.child_type,
                    status="timeout",
                    summary=f"Timed out after {timeout_s}s (cooperative cancel requested).",
                    raw=raw_snapshot,
                )
            )
            self._job_status[spec.job_id] = "timeout"
            self._terminal_job_ids.add(spec.job_id)
            self._result_index_by_job_id.setdefault(spec.job_id, len(self._results) - 1)

    def _request_cancel_instance(self, inst: object) -> None:
        """
        Cooperative cancel contract:
        - If the child exposes .cancel(), call it.
        - If it has a .blackboard, set blackboard['cancelled']=True (global), so its loop can exit.
        """
        if inst is None:
            return
        try:
            cancel = getattr(inst, "cancel", None)
            if callable(cancel):
                cancel()
        except Exception as e:
            self._log_debug_exception("child_cancel_call_failed", e)
        try:
            bb = getattr(inst, "blackboard", None)
            if bb is not None:
                setter = getattr(bb, "update_global_state_value", None)
                if callable(setter):
                    setter("cancelled", True)
                else:
                    # fallback: local scope
                    setter2 = getattr(bb, "update_state_value", None)
                    if callable(setter2):
                        setter2("cancelled", True)
        except Exception as e:
            self._log_debug_exception("child_cancel_flag_set_failed", e)

