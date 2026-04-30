"""
Bucket-aware parallel runner for the KG pipeline.

One daemon thread per step. Each thread loops:
    step.run_one_cycle(ctx) → sleep poll_interval → repeat

Auto-stops after `max_idle_rounds` consecutive cycles where every worker
reports zero work done.

Each step exposes:
    name: str
    run_one_cycle(ctx) -> int    # returns count of items processed this cycle
                                 # 0 means "input bucket was empty"
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class PipelineStep(Protocol):
    name: str
    def run_one_cycle(self, ctx: Any) -> int: ...
    # Optional: number of source rows still waiting to be processed by this
    # step. The runner's monitor uses this to keep workers alive while ANY
    # stage's input bucket is non-empty — even if no items have been
    # processed in the last interval (e.g. the segmenter sitting on its
    # quiet-period gate). Steps that don't implement this fall back to the
    # processed-delta heuristic.
    def pending_count(self) -> int: ...


@dataclass
class WorkerStats:
    step_name: str
    iterations: int = 0
    items_processed: int = 0
    errors: int = 0
    db_lock_retries: int = 0
    total_duration_s: float = 0.0


@dataclass
class PipelineRunResult:
    pipeline_id: str
    run_id: str
    date: str
    status: str
    workers: List[Dict[str, Any]] = field(default_factory=list)


def _is_database_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or isinstance(exc, sqlite3.OperationalError)


class KGPipelineRunner:
    """
    One daemon thread per step. No multi-worker-per-stage parallelism.
    The 6× concurrency comes from stages running in parallel.
    """

    def __init__(
        self,
        *,
        steps: List[PipelineStep],
        poll_interval: float = 5.0,
        db_lock_backoff: float = 3.0,
    ) -> None:
        self._steps = steps
        self._poll_interval = poll_interval
        self._db_lock_backoff = db_lock_backoff
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._worker_stats: Dict[str, WorkerStats] = {}

    def run(
        self,
        ctx: Any,
        *,
        max_idle_rounds: int = 5,
        monitor_interval: float = 15.0,
    ) -> PipelineRunResult:
        self._stop_event.clear()
        self._worker_stats.clear()

        threads: List[threading.Thread] = []
        for step in self._steps:
            stats = WorkerStats(step_name=step.name)
            with self._lock:
                self._worker_stats[step.name] = stats
            t = threading.Thread(
                target=self._worker_loop,
                args=(step, ctx, stats),
                name=f"kg-pipeline-{step.name}",
                daemon=True,
            )
            threads.append(t)

        for t in threads:
            t.start()
            logger.info("[kg_pipeline] started worker thread: %s", t.name)

        overall_status = self._monitor_loop(
            max_idle_rounds=max_idle_rounds,
            monitor_interval=monitor_interval,
        )

        self._stop_event.set()
        for t in threads:
            t.join(timeout=30)

        with self._lock:
            worker_dicts = [
                {
                    "step": s.step_name,
                    "iterations": s.iterations,
                    "items_processed": s.items_processed,
                    "errors": s.errors,
                    "db_lock_retries": s.db_lock_retries,
                    "total_duration_s": round(s.total_duration_s, 2),
                }
                for s in self._worker_stats.values()
            ]

        return PipelineRunResult(
            pipeline_id=getattr(ctx, "pipeline_id", "kg_pipeline"),
            run_id=getattr(ctx, "run_id", ""),
            date=getattr(ctx, "date_str", ""),
            status=overall_status,
            workers=worker_dicts,
        )

    def stop(self) -> None:
        self._stop_event.set()

    def get_live_stats(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {
                name: {
                    "iterations": s.iterations,
                    "items_processed": s.items_processed,
                    "errors": s.errors,
                    "db_lock_retries": s.db_lock_retries,
                }
                for name, s in self._worker_stats.items()
            }

    # ------------------------------------------------------------------
    # Worker loop (one per step, runs in own thread)
    # ------------------------------------------------------------------
    def _worker_loop(self, step: PipelineStep, ctx: Any, stats: WorkerStats) -> None:
        thread_name = threading.current_thread().name
        logger.info("[kg_pipeline] %s: worker started", thread_name)

        while not self._stop_event.is_set():
            t0 = time.monotonic()
            try:
                processed = step.run_one_cycle(ctx)
                elapsed = time.monotonic() - t0
                with self._lock:
                    stats.iterations += 1
                    stats.items_processed += int(processed or 0)
                    stats.total_duration_s += elapsed
            except Exception as exc:
                elapsed = time.monotonic() - t0
                is_db_lock = _is_database_locked(exc)
                with self._lock:
                    stats.iterations += 1
                    stats.total_duration_s += elapsed
                    if is_db_lock:
                        stats.db_lock_retries += 1
                    else:
                        stats.errors += 1
                if is_db_lock:
                    logger.warning(
                        "[kg_pipeline] %s: database locked, backing off %.1fs",
                        thread_name, self._db_lock_backoff,
                    )
                    self._stop_event.wait(self._db_lock_backoff)
                    continue
                logger.exception(
                    "[kg_pipeline] %s: step.run_one_cycle() raised %s: %s",
                    thread_name, type(exc).__name__, exc,
                )

            self._stop_event.wait(self._poll_interval)

        logger.info("[kg_pipeline] %s: worker stopped", thread_name)

    # ------------------------------------------------------------------
    # Monitor loop (runs in main thread)
    # ------------------------------------------------------------------
    def _sum_pending(self) -> int:
        """Sum pending-input counts across all steps that expose pending_count.
        Steps without the method contribute 0; the monitor will then fall
        back to the processed-delta signal for those stages."""
        total = 0
        for step in self._steps:
            fn = getattr(step, "pending_count", None)
            if not callable(fn):
                continue
            try:
                total += int(fn() or 0)
            except Exception as exc:
                logger.warning(
                    "[kg_pipeline] pending_count failed for step=%s: %s",
                    getattr(step, "name", "?"), exc,
                )
        return total

    def _monitor_loop(
        self,
        *,
        max_idle_rounds: int,
        monitor_interval: float,
    ) -> str:
        idle_rounds = 0
        last_processed_total = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(monitor_interval)
            with self._lock:
                processed_now = sum(s.items_processed for s in self._worker_stats.values())
                error_count = sum(s.errors for s in self._worker_stats.values())
            delta = processed_now - last_processed_total
            last_processed_total = processed_now
            pending = self._sum_pending()
            if delta == 0 and pending == 0:
                idle_rounds += 1
                logger.info(
                    "[kg_pipeline] monitor: idle round %d/%d (processed_total=%d pending=0 errors=%d)",
                    idle_rounds, max_idle_rounds, processed_now, error_count,
                )
                if idle_rounds >= max_idle_rounds:
                    logger.info("[kg_pipeline] monitor: max idle rounds reached, stopping")
                    return "completed_idle"
            elif delta == 0:
                # Pending work exists somewhere upstream — keep workers alive,
                # do NOT advance the idle counter. Reset so a transient drain
                # from a slow stage doesn't trigger premature shutdown.
                idle_rounds = 0
                logger.info(
                    "[kg_pipeline] monitor: waiting on gated input (processed_total=%d pending=%d errors=%d)",
                    processed_now, pending, error_count,
                )
            else:
                idle_rounds = 0
                logger.info(
                    "[kg_pipeline] monitor: processed +%d (total=%d pending=%d errors=%d)",
                    delta, processed_now, pending, error_count,
                )
        return "stopped"
