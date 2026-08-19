"""
Dojo — the training loop. Runs a task repeatedly, analyzing traces and refining
after each run. Train in the dojo; sensei reviews the form.

Usage:
    from app.assistant.execution_trace.dojo import dojo_run
    dojo_run("doordash_pizza_hut", max_runs=5, run_timeout=300)
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def dojo_run(
    task_id: str,
    *,
    max_runs: int = 5,
    run_timeout: int = 300,
    artifact_dir: str = "data/execution_traces",
    refine: bool = True,
) -> Dict[str, Any]:
    """
    Run a task repeatedly in the dojo, analyzing and learning after each run.

    Returns a summary of all runs.
    """
    task_dir = Path("tasks") / task_id
    task_spec_path = task_dir / "task_spec.md"
    if not task_spec_path.exists():
        raise FileNotFoundError(f"Task spec not found: {task_spec_path}")

    # Initialize versioning — save the starting spec as v1 if no versions exist yet.
    spec_version = _ensure_baseline_version(task_dir)
    task_spec_text = task_spec_path.read_text(encoding="utf-8")
    results = []
    cart_items = 0  # Track cumulative cart state across runs.

    for run_number in range(1, max_runs + 1):
        logger.info("=" * 60)
        logger.info("[dojo] RUN %d/%d for task '%s' (spec v%d)", run_number, max_runs, task_id, spec_version)
        logger.info("=" * 60)

        # 1. Execute the task
        run_result = _execute_run(
            task_id, task_spec_text,
            run_timeout=run_timeout, spec_version=spec_version,
            run_number=run_number, cart_items=cart_items,
        )
        results.append(run_result)
        if run_result.get("success"):
            cart_items += 1

        # 2. Find the trace artifact from this run
        trace_path = _find_latest_trace(artifact_dir, task_id)
        if not trace_path:
            logger.warning("[dojo] No trace artifact found for run %d — skipping analysis.", run_number)
            continue

        trace_doc = json.loads(trace_path.read_text(encoding="utf-8"))
        logger.info(
            "[dojo] Run %d: %d steps, %d critic interventions, success=%s",
            run_number,
            trace_doc.get("total_steps", 0),
            trace_doc.get("total_critic_interventions", 0),
            trace_doc.get("success", False),
        )

        # 3. Analyze the trace
        analysis = _analyze_trace(task_spec_text, trace_doc)
        if analysis:
            _save_analysis(artifact_dir, task_id, run_number, analysis)
            logger.info(
                "[dojo] Analysis: deterministic=%.0f%%, suggestions=%d",
                analysis.get("estimated_deterministic_pct", 0),
                len(analysis.get("optimization_suggestions", [])),
            )

            # 4. Refine the spec using analyst findings — save as new version
            updated_spec = _refine_spec(task_spec_text, analysis, trace_doc) if refine else None
            if updated_spec:
                spec_version = _save_spec_version(task_dir, updated_spec)
                task_spec_text = updated_spec
                logger.info("[dojo] Spec refined → v%d saved for next run.", spec_version)

        # Brief pause between runs
        if run_number < max_runs:
            logger.info("[dojo] Pausing 5s before next run...")
            time.sleep(5)

    # Summary
    summary = {
        "task_id": task_id,
        "total_runs": len(results),
        "successful_runs": sum(1 for r in results if r.get("success")),
        "final_spec_version": spec_version,
        "runs": results,
    }
    logger.info(
        "[dojo] COMPLETE: %d/%d runs successful for task '%s' (spec v%d)",
        summary["successful_runs"],
        summary["total_runs"],
        task_id,
        spec_version,
    )
    return summary


def _get_latest_spec_version(task_dir: Path) -> int:
    """Return the highest version number in specs/, or 0 if no versions exist."""
    specs_dir = task_dir / "specs"
    if not specs_dir.exists():
        return 0
    versions = []
    for f in specs_dir.glob("v*.md"):
        try:
            versions.append(int(f.stem[1:]))
        except ValueError:
            continue
    return max(versions) if versions else 0


def _ensure_baseline_version(task_dir: Path) -> int:
    """Save the current task_spec.md as v1 if no versions exist yet. Returns current version."""
    latest = _get_latest_spec_version(task_dir)
    if latest > 0:
        return latest
    spec_path = task_dir / "task_spec.md"
    if not spec_path.exists():
        return 0
    specs_dir = task_dir / "specs"
    specs_dir.mkdir(exist_ok=True)
    specs_dir.joinpath("v1.md").write_text(spec_path.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info("[dojo] Saved baseline spec as v1.md")
    return 1


def _save_spec_version(task_dir: Path, spec_text: str) -> int:
    """Save a new spec version and update task_spec.md. Returns the new version number."""
    specs_dir = task_dir / "specs"
    specs_dir.mkdir(exist_ok=True)
    new_version = _get_latest_spec_version(task_dir) + 1
    specs_dir.joinpath(f"v{new_version}.md").write_text(spec_text, encoding="utf-8")
    # Update the active spec so execution mode uses the latest.
    task_dir.joinpath("task_spec.md").write_text(spec_text, encoding="utf-8")
    return new_version


def _execute_run(
    task_id: str, task_spec_text: str,
    run_timeout: int = 300, spec_version: int = 0,
    run_number: int = 1, cart_items: int = 0,
) -> Dict[str, Any]:
    """Execute one dojo run of the task with a hard timeout."""
    import os
    import threading
    import concurrent.futures

    t0 = time.monotonic()
    # Bypass tool approval in the dojo — no human in the loop.
    old_bypass = os.environ.get("EMI_BYPASS_APPROVAL")
    os.environ["EMI_BYPASS_APPROVAL"] = "1"

    # Build dojo context so the planner knows about cumulative state.
    info_parts = [f"Dojo run {run_number} for task {task_id}. STOP before placing any real order."]
    if cart_items > 0:
        info_parts.append(
            f"The cart already contains {cart_items} item(s) from previous runs. "
            f"The expected cart count after this run is {cart_items + 1}."
        )

    def _run_manager():
        from app.assistant.utils.pydantic_classes import ScopeContext

        scope = ScopeContext(
            scope_id=f"dojo_{task_id}",
            owner_id="system",
            actor_id="dojo",
            surface="ide",
            room_id="dojo",
            resources={"allowed_global_resources": ["all"]},
        )
        msg = Message(
            task=task_spec_text,
            information="\n".join(info_parts),
            data={"task_id": task_id, "spec_version": spec_version, "run_number": run_number},
            scope_context=scope,
        )
        manager = DI.multi_agent_manager_factory.create_manager("playwright_manager")
        return DI.manager_invoker.invoke(manager, msg)

    # NOT a `with` block. Returning from inside one calls
    # ThreadPoolExecutor.__exit__ -> shutdown(wait=True), which blocks until the
    # worker finishes -- so the timeout below bounded nothing and a hung run hung
    # forever regardless of run_timeout. Measured: worker 6s, timeout 1s, the
    # TimeoutError fired at 1.00s but the caller did not regain control until
    # 6.00s.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_run_manager)
        try:
            result = future.result(timeout=run_timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("[dojo] Run timed out after %ds", run_timeout)
            # Abandon the worker deliberately so the caller regains control at
            # the deadline and can record the timeout / move to the next run.
            #
            # This does NOT stop the run. The future cannot be cancelled once
            # started, and since 3.9 concurrent.futures registers an atexit hook
            # that joins worker threads, so the PROCESS still waits for it at
            # interpreter exit (measured: caller free at 1.02s, process exit at
            # 6.08s). Genuinely stopping it needs a cooperative cancellation
            # signal the manager checks -- which the comment previously here
            # claimed ("signal the manager to stop via blackboard cancellation")
            # but which was never implemented; grep found no such signal.
            pool.shutdown(wait=False)
            return {
                "task_id": task_id,
                "spec_version": spec_version,
                "success": False,
                "elapsed_seconds": round(time.monotonic() - t0, 1),
                "error": f"Timed out after {run_timeout}s",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        pool.shutdown(wait=True)

        elapsed = time.monotonic() - t0
        success = False
        answer = ""
        if hasattr(result, "data") and isinstance(result.data, dict):
            success = not result.data.get("error", False)
            answer = str(result.data.get("final_answer_answer") or result.content or "")[:500]
        elif hasattr(result, "content"):
            answer = str(result.content or "")[:500]
            success = "error" not in answer.lower()

        return {
            "task_id": task_id,
            "spec_version": spec_version,
            "success": success,
            "elapsed_seconds": round(elapsed, 1),
            "answer_preview": answer,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        elapsed = time.monotonic() - t0
        logger.error("[dojo] Run failed: %s", e, exc_info=True)
        return {
            "task_id": task_id,
            "spec_version": spec_version,
            "success": False,
            "elapsed_seconds": round(elapsed, 1),
            "error": str(e)[:300],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        # Never block here: on the timeout path we have deliberately abandoned
        # the worker, and on the error path we do not want to inherit its wait.
        pool.shutdown(wait=False)
        if old_bypass is None:
            os.environ.pop("EMI_BYPASS_APPROVAL", None)
        else:
            os.environ["EMI_BYPASS_APPROVAL"] = old_bypass


def _find_latest_trace(artifact_dir: str, task_id: str) -> Optional[Path]:
    """Find the most recent trace artifact for a task (excludes analysis files)."""
    task_dir = Path(artifact_dir) / task_id
    if not task_dir.exists():
        return None
    # Trace files are UUIDs, analysis files start with "analysis_"
    traces = sorted(
        [f for f in task_dir.glob("*.json") if not f.name.startswith("analysis_")],
        key=lambda p: p.stat().st_mtime,
    )
    return traces[-1] if traces else None


def _analyze_trace(task_spec_text: str, trace_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Run the sensei::analyst agent on the execution trace."""
    try:
        analyst = DI.agent_factory.create_agent("sensei::analyst")

        # Format the trace for the analyst
        trace_summary = json.dumps(trace_doc, indent=2, default=str)

        # Truncate if too large (keep steps, drop verbose previews)
        if len(trace_summary) > 50000:
            compact_doc = dict(trace_doc)
            for step in compact_doc.get("steps", []):
                if step.get("tool_result_preview") and len(step["tool_result_preview"]) > 200:
                    step["tool_result_preview"] = step["tool_result_preview"][:200] + "..."
                if step.get("reasoning") and len(step["reasoning"]) > 200:
                    step["reasoning"] = step["reasoning"][:200] + "..."
            trace_summary = json.dumps(compact_doc, indent=2, default=str)

        msg = Message(
            agent_input={
                "task": task_spec_text,
                "information": trace_summary,
            },
        )
        result = analyst.action_handler(msg)

        if hasattr(result, "data") and isinstance(result.data, dict):
            return result.data
        return None
    except Exception as e:
        logger.error("[dojo] Analysis failed: %s", e, exc_info=True)
        return None


def _refine_spec(
    current_spec: str,
    analysis: Dict[str, Any],
    trace_doc: Dict[str, Any],
) -> Optional[str]:
    """Run the sensei::refiner agent to update the spec with analyst findings."""
    try:
        refiner = DI.agent_factory.create_agent("sensei::refiner")

        # Combine analysis + trace steps into a single context for the refiner
        findings = {
            "analysis": analysis,
            "trace_steps": trace_doc.get("steps", []),
            "trace_critic_interventions": trace_doc.get("critic_interventions", []),
            "duration_seconds": trace_doc.get("duration_seconds"),
            "total_steps": trace_doc.get("total_steps"),
        }
        findings_text = json.dumps(findings, indent=2, default=str)

        # Truncate if too large
        if len(findings_text) > 50000:
            for step in findings.get("trace_steps", []):
                if step.get("tool_result_preview") and len(step["tool_result_preview"]) > 100:
                    step["tool_result_preview"] = step["tool_result_preview"][:100] + "..."
            findings_text = json.dumps(findings, indent=2, default=str)

        msg = Message(
            agent_input={
                "task": current_spec,
                "information": findings_text,
            },
        )
        result = refiner.action_handler(msg)

        if hasattr(result, "data") and isinstance(result.data, dict):
            updated = result.data.get("updated_spec", "").strip()
            changes = result.data.get("changes_summary", "")
            if updated:
                logger.info("[dojo] Spec refined: %s", changes)
                return updated
        return None
    except Exception as e:
        logger.error("[dojo] Spec refinement failed: %s", e, exc_info=True)
        return None


def _save_analysis(artifact_dir: str, task_id: str, run_number: int, analysis: Dict[str, Any]) -> None:
    """Save the analysis alongside the trace."""
    try:
        task_dir = Path(artifact_dir) / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / f"analysis_run_{run_number}.json"
        path.write_text(json.dumps(analysis, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        logger.info("[dojo] Saved analysis: %s", path)
    except Exception as e:
        logger.error("[dojo] Failed to save analysis: %s", e, exc_info=True)
