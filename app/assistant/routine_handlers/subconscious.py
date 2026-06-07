"""Routine handlers for the subconscious brain.

Each handler wraps one of the ``app/assistant/subconscious/run_*.py``
CLI entry points so it can run as a declarative routine (interval +
active_window or weekly day-of-week) without anyone typing the command.

The flow is uniform across all of them:

    1. build_<X>_context(...)       → context dict for the agent
    2. DI.agent_factory.create_agent(<agent_name>)
    3. agent.action_handler(Message(agent_input=context, scope_context=...))
    4. apply_<X>_output(result.data) → persist concerns / intentions / etc.
    5. return a small summary dict (visible in routine_manager's decision log)

Corresponding routine JSON entries live in:
    configs/routines/public/<routine_id>.json

Cadence (local times — see each json for the canonical config):

    04:00–05:00   noticer                       (Pass A inward + Pass B outward)
    05:00–05:30   daily_meal_proposer           (reads concerns + minted)
    05:00–05:30   wellness_proposer             (parallel; reads same concerns)
    05:00–05:30   romantic_proposer             (parallel)
    05:30–06:00   scheduler_arbiter             (after the three proposers)
    Sun 17:00     weekly_meal_planner           (sets the upcoming week)
    Sun 22:00     skill_distiller               (long-loop learning)
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.assistant.routine_handlers import routine_handler
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _run_subconscious_agent(
    *,
    handler_label: str,
    agent_name: str,
    context: Dict[str, Any],
    scope_id: str,
    actor_id: str,
) -> Dict[str, Any]:
    """Build a scope, invoke the agent, return the structured output dict.

    Returns the agent's ``result.data`` if it's a dict, otherwise the
    raw result for caller-side inspection. Raises on agent error so the
    routine_manager can record a failure (it surfaces in its decision log
    + counts toward on_error.max_failures).
    """
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        raise RuntimeError(f"[{handler_label}] could not create agent {agent_name!r}")

    scope = load_scope_for_source(
        kind="subsystem",
        source_id="subconscious",
        actor_id=actor_id,
        identity_overrides={"owner_id": "system", "scope_id": scope_id, "surface": "internal"},
    )
    result = agent.action_handler(Message(agent_input=context, scope_context=scope))

    if hasattr(result, "data") and isinstance(result.data, dict):
        return result.data
    if isinstance(result, dict):
        return result
    raise RuntimeError(
        f"[{handler_label}] unexpected agent result type: {type(result).__name__}"
    )


# ---------------------------------------------------------------------------
# Noticer
# ---------------------------------------------------------------------------


@routine_handler(name="noticer_run")
def noticer_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Run one noticer pass. Default trigger_mode='daily'."""
    from app.assistant.subconscious.context_builder import build_noticer_context
    from app.assistant.subconscious.persist import apply_noticer_output

    context = build_noticer_context(trigger_mode="daily")
    output = _run_subconscious_agent(
        handler_label="noticer_run",
        agent_name="subconscious::noticer",
        context=context,
        scope_id="subconscious::noticer",
        actor_id="routine::noticer_run",
    )
    summary = apply_noticer_output(output) or {}
    logger.info(
        "[noticer_run] new=%d reinforced=%d resolved=%d escalated=%d beliefs=%d questions=%d",
        len(output.get("new_concerns") or []),
        len(output.get("reinforced_concerns") or []),
        len(output.get("resolved_concerns") or []),
        len(output.get("escalated_concerns") or []),
        len(output.get("belief_updates") or []),
        len(output.get("pending_questions") or []),
    )
    return {"status": "ok", **summary}


# ---------------------------------------------------------------------------
# Daily meal proposer
# ---------------------------------------------------------------------------


@routine_handler(name="daily_meal_proposer_run")
def daily_meal_proposer_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    from app.assistant.subconscious.meal_context_builder import (
        build_daily_meal_proposer_context,
    )
    from app.assistant.subconscious.meal_persist import apply_daily_meal_proposer_output

    context = build_daily_meal_proposer_context()
    output = _run_subconscious_agent(
        handler_label="daily_meal_proposer_run",
        agent_name="daily_meal_proposer",
        context=context,
        scope_id="subconscious::daily_meal_proposer",
        actor_id="routine::daily_meal_proposer_run",
    )
    summary = apply_daily_meal_proposer_output(output) or {}
    logger.info(
        "[daily_meal_proposer_run] proposals=%d shopping_pod=%s set_pod=%s",
        summary.get("proposal_pod_count") or 0,
        summary.get("shopping_pod_id") or "(none)",
        summary.get("set_pod_id") or "(none)",
    )
    return {"status": "ok", **summary}


@routine_handler(name="grocery_sync_run")
def grocery_sync_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Daily: scan recent chat for grocery intents (bought / ran out / consumed), apply
    them to the inventory, and run decay — so the inventory_snapshot fed to the meal
    planners stays fresh. Was CLI-only before, so inventory never updated and decay never
    ran (see scratch/MEAL-PLANNING-AUDIT.md)."""
    from app.assistant.subconscious.grocery_sync_runner import run_grocery_sync_pass

    summary = run_grocery_sync_pass(hours=48)
    logger.info(
        "[grocery_sync_run] intents_detected=%d intents_applied=%d decay=%s",
        summary.get("intents_detected") or 0,
        summary.get("intents_applied") or 0,
        summary.get("decay"),
    )
    return {"status": "ok", **summary}


# ---------------------------------------------------------------------------
# Wellness proposer
# ---------------------------------------------------------------------------


@routine_handler(name="wellness_proposer_run")
def wellness_proposer_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    from app.assistant.subconscious.wellness_context_builder import (
        build_wellness_proposer_context,
    )
    from app.assistant.subconscious.wellness_persist import apply_wellness_proposer_output

    context = build_wellness_proposer_context()
    output = _run_subconscious_agent(
        handler_label="wellness_proposer_run",
        agent_name="wellness_proposer",
        context=context,
        scope_id="subconscious::wellness_proposer",
        actor_id="routine::wellness_proposer_run",
    )
    summary = apply_wellness_proposer_output(output) or {}
    logger.info(
        "[wellness_proposer_run] intentions=%d",
        len(output.get("intentions") or []),
    )
    return {"status": "ok", **summary}


# ---------------------------------------------------------------------------
# Romantic proposer
# ---------------------------------------------------------------------------


@routine_handler(name="romantic_proposer_run")
def romantic_proposer_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    from app.assistant.subconscious.romantic_context_builder import (
        build_romantic_proposer_context,
    )
    from app.assistant.subconscious.romantic_persist import apply_romantic_proposer_output

    context = build_romantic_proposer_context()
    output = _run_subconscious_agent(
        handler_label="romantic_proposer_run",
        agent_name="romantic_proposer",
        context=context,
        scope_id="subconscious::romantic_proposer",
        actor_id="routine::romantic_proposer_run",
    )
    summary = apply_romantic_proposer_output(output) or {}
    logger.info(
        "[romantic_proposer_run] intentions=%d skipped=%d",
        len(output.get("intentions") or []),
        len(output.get("skipped") or []),
    )
    return {"status": "ok", **summary}


# ---------------------------------------------------------------------------
# Scheduler arbiter
# ---------------------------------------------------------------------------


@routine_handler(name="scheduler_arbiter_run")
def scheduler_arbiter_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """The arbiter consumes the day's intention.* pods and emits a
    single plan.weekly_schedule. Runs AFTER the three proposers."""
    from app.assistant.subconscious.scheduler_arbiter_context_builder import (
        build_scheduler_arbiter_context,
    )
    from app.assistant.subconscious.scheduler_arbiter_persist import (
        apply_scheduler_arbiter_output,
    )

    context = build_scheduler_arbiter_context()
    output = _run_subconscious_agent(
        handler_label="scheduler_arbiter_run",
        agent_name="scheduler_arbiter",
        context=context,
        scope_id="subconscious::scheduler_arbiter",
        actor_id="routine::scheduler_arbiter_run",
    )
    summary = apply_scheduler_arbiter_output(output) or {}
    logger.info(
        "[scheduler_arbiter_run] scheduled=%d conflicts=%d punted=%d",
        len(output.get("scheduled") or []),
        len(output.get("conflicts") or []),
        len(output.get("punted") or []),
    )
    return {"status": "ok", **summary}


# ---------------------------------------------------------------------------
# Weekly meal planner
# ---------------------------------------------------------------------------


@routine_handler(name="weekly_meal_planner_run")
def weekly_meal_planner_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Sets the UPCOMING week's meal plan (Sunday evening, before the new week starts).

    Runs the full chain (distiller -> planning manager -> weekly_meal_planner) so the
    autonomous plan gets the same distilled meal_context the /meals button does — the old
    direct-agent call skipped the distiller, leaving the planner blind to dietary /
    concerns / audience (see scratch/MEAL-PLANNING-AUDIT.md §6). Defaults to the upcoming
    Monday (target_date overrides, e.g. for a manual/backfill run)."""
    from app.assistant.subconscious.weekly_meal_planning_runner import (
        run_weekly_meal_planning_chain,
        upcoming_monday_iso,
    )

    week_start = target_date or upcoming_monday_iso()
    summary = run_weekly_meal_planning_chain(week_start=week_start, persist=True)
    logger.info(
        "[weekly_meal_planner_run] week_start=%s plan_pod=%s meal_context_days=%d",
        summary.get("week_start_date"),
        summary.get("plan_pod_id") or "(none)",
        summary.get("meal_context_days") or 0,
    )
    return {"status": "ok", **summary}


# ---------------------------------------------------------------------------
# Skill distiller
# ---------------------------------------------------------------------------


@routine_handler(name="skill_distiller_run")
def skill_distiller_run(
    *,
    target_date: Optional[str] = None,
    routine: Any = None,
    event_message: Any = None,
) -> Dict[str, Any]:
    """Weekly long-loop learning pass. Reviews intention.* pods + arbiter
    decisions + chat outcomes and PROPOSES rule additions to canonical
    task_spec files. Does not auto-apply — proposals land in
    ``resources/subconscious/resource_learned_skills_proposed.md`` for
    manual review."""
    from app.assistant.subconscious.skill_distiller_context_builder import (
        build_skill_distiller_context,
    )
    from app.assistant.subconscious.skill_distiller_persist import (
        apply_skill_distiller_output,
    )

    context = build_skill_distiller_context()
    output = _run_subconscious_agent(
        handler_label="skill_distiller_run",
        agent_name="skill_distiller",
        context=context,
        scope_id="subconscious::skill_distiller",
        actor_id="routine::skill_distiller_run",
    )
    summary = apply_skill_distiller_output(output) or {}
    logger.info(
        "[skill_distiller_run] proposed=%d skipped=%d",
        len(output.get("proposals") or []),
        len(output.get("skipped") or []),
    )
    return {"status": "ok", **summary}
