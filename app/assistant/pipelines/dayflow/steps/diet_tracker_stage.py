"""
Diet Tracker Step

Purpose:
- Maintain a running log of what the user has eaten and drunk today.
- Incremental: runs ONLY when a new food-tagged pod has landed since the
  last run. On every fire, pulls the new pods, merges them into
  ``resource_diet_log_today.json``.
- Coexists with activity_tracker, which keeps tracking counts/timers.
  This step tracks CONTENT ("what was eaten"), not counts.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import (
    parse_iso_utc,
    utc_to_local,
    get_local_time_str,
)
from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore

logger = get_logger(__name__)

_OUTPUT_FILENAME = "resource_diet_log_today.json"
_OUTPUT_MD_FILENAME = "resource_diet_log_today_text.md"
_FOOD_TAGS: tuple[str, ...] = ("food",)
_FIRST_RUN_LOOKBACK_HOURS = 24
# status_effects on cron_reminder tickets that are diet-relevant. Matches
# activity_tracker's countable activities.
_DIET_TICKET_EFFECTS: frozenset[str] = frozenset({"meal", "snack", "hydration", "coffee"})


class DietTrackerStep(BaseStep):
    step_id: str = "diet_tracker"

    def __init__(self) -> None:
        self._store: PodStore | None = None

    def _ensure_store(self) -> PodStore:
        if self._store is None:
            self._store = PodStore()
        return self._store

    def _since_for_query(self, ctx: StepContext) -> datetime:
        """On first run, look back a day so we don't miss breakfast;
        afterwards use the last-run cursor."""
        last = self._get_last_run_utc(ctx)
        if last is not None:
            return last
        return ctx.now_utc - timedelta(hours=_FIRST_RUN_LOOKBACK_HOURS)

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        # Safety floor so bugs can't make us hammer.
        step_cfg = ctx.step_config or {}
        run_policy = step_cfg.get("run_policy", {}) if isinstance(step_cfg, dict) else {}
        min_interval = int(run_policy.get("min_interval_seconds", 60))
        last_run = self._get_last_run_utc(ctx)
        if last_run:
            elapsed = (ctx.now_utc - last_run).total_seconds()
            if elapsed < min_interval:
                return False, f"interval={int(min_interval - elapsed)}s remaining"

        # Reactive gate: run if EITHER a new food-tagged pod OR a new
        # diet-relevant ticket has landed since last run. Cheap probes.
        since = self._since_for_query(ctx)
        try:
            probe = self._ensure_store().query(
                tags=list(_FOOD_TAGS),
                since=since,
                limit=1,
            )
        except Exception as e:
            logger.warning("DietTrackerStep: pod_store probe failed: %s", e)
            return False, "pod_store_probe_failed"
        if probe:
            return True, f"new_food_pod ({probe[0].pod_id[-12:]})"

        # No new pods — check diet-relevant tickets.
        try:
            new_tickets = _fetch_diet_tickets_since(since)
        except Exception as e:
            logger.warning("DietTrackerStep: ticket probe failed: %s", e)
            return False, "ticket_probe_failed"
        if new_tickets:
            return True, f"new_diet_ticket ({new_tickets[0].ticket_id})"
        return False, "no_new_food_pods_or_tickets"

    def run(self, ctx: StepContext) -> StepResult:
        now_utc = ctx.now_utc
        since = self._since_for_query(ctx)

        # 1. Pull new food pods since last run.
        store = self._ensure_store()
        new_pods = store.query(tags=list(_FOOD_TAGS), since=since, limit=50)

        # 2. Load current log (or seed a fresh one).
        current_log = ctx.read_resource(_OUTPUT_FILENAME) or {
            "schema_version": 1,
            "date_local": ctx.now_local.strftime("%Y-%m-%d"),
            "items": [],
            "summary": "",
            "open_questions": [],
            "updated_at_utc": None,
        }
        # Reset if the archived log is from a prior day (defensive).
        today_local = ctx.now_local.strftime("%Y-%m-%d")
        if str(current_log.get("date_local") or "") != today_local:
            logger.info(
                "DietTrackerStep: stale log date (%s != %s), starting fresh",
                current_log.get("date_local"), today_local,
            )
            current_log = {
                "schema_version": 1,
                "date_local": today_local,
                "items": [],
                "summary": "",
                "open_questions": [],
                "updated_at_utc": None,
            }

        # 3. Pull accepted diet-related tickets since last run. A ticket may
        # refer to the SAME eating event as a chat pod ("I'll grab coffee"
        # followed a few minutes later by "got my coffee"); the agent is
        # told to merge these with a common-sense time window.
        diet_tickets = _fetch_diet_tickets_since(since)

        # 4. Render pods + tickets for the agent.
        new_food_pods_block = _render_pods_for_agent(new_pods)
        new_tickets_block = _render_tickets_for_agent(diet_tickets)

        # 5. Call the agent.
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message

        agent = DI.agent_factory.create_agent("diet_tracker")
        if agent is None:
            raise RuntimeError("diet_tracker agent unavailable")

        scope_context = load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=f"{self.step_id}_runner")
        msg = Message(
            scope_context=scope_context,
            agent_input={
                "date_time": get_local_time_str(),
                "current_diet_log": current_log,
                "new_food_pods_block": new_food_pods_block,
                "new_tickets_block": new_tickets_block,
            },
        )
        result = agent.action_handler(msg)
        data = result.data if hasattr(result, "data") and isinstance(result.data, dict) else {}

        # 5. Merge into the canonical log shape and write.
        merged = {
            "schema_version": 1,
            "date_local": today_local,
            "items": data.get("items") or [],
            "summary": str(data.get("summary") or ""),
            "open_questions": data.get("open_questions") or [],
            "updated_at_utc": now_utc.isoformat(),
            "last_reasoning": str(data.get("reasoning") or ""),
        }
        ctx.write_resource(_OUTPUT_FILENAME, merged)
        _write_markdown_view(ctx, merged)

        return StepResult(
            output={"last_run_utc": now_utc.isoformat(), "status": "ok"},
            debug={
                "since": since.isoformat(),
                "new_pods": len(new_pods),
                "new_tickets": len(diet_tickets),
                "item_count": len(merged["items"]),
            },
        )

    def reset(self, ctx: StepContext) -> None:
        """Reset the log at the daily boundary. Always seeds an empty log so
        the resource exists for downstream consumers before any food pod
        has triggered the step for the new day."""
        today_local = ctx.now_local.strftime("%Y-%m-%d")
        empty = {
            "schema_version": 1,
            "date_local": today_local,
            "items": [],
            "summary": "",
            "open_questions": [],
            "updated_at_utc": ctx.now_utc.isoformat(),
        }
        ctx.write_resource(_OUTPUT_FILENAME, empty)
        _write_markdown_view(ctx, empty)
        logger.info("DietTrackerStep: reset for new day %s", today_local)


def _fetch_diet_tickets_since(since_utc: datetime) -> List[Any]:
    """Pull cron_reminder tickets with a diet-relevant status_effect that
    were RESPONDED (accepted/dismissed) or CREATED since ``since_utc``.
    Includes dismissed because a dismiss ("not yet") is useful negative
    evidence the agent can reason about.
    """
    try:
        from app.assistant.ticket_manager import get_ticket_manager, TicketState
        tm = get_ticket_manager()
        # Responded-since catches user interactions; created-since backfills
        # ones we missed. Merge + dedup by ticket_id.
        responded = tm.get_tickets(
            ticket_type="cron_reminder",
            since_responded_utc=since_utc,
            states=[TicketState.ACCEPTED, TicketState.DISMISSED, TicketState.COMPLETED],
            limit=50,
        )
        created = tm.get_tickets(
            ticket_type="cron_reminder",
            since_utc=since_utc,
            states=[TicketState.ACCEPTED, TicketState.DISMISSED, TicketState.COMPLETED],
            limit=50,
        )
        seen: set = set()
        merged: List[Any] = []
        for t in list(responded) + list(created):
            if t.ticket_id in seen:
                continue
            effects = set(t.status_effect or []) if isinstance(t.status_effect, list) else set()
            if not (effects & _DIET_TICKET_EFFECTS):
                continue
            seen.add(t.ticket_id)
            merged.append(t)
        merged.sort(key=lambda t: (t.responded_at or t.created_at))
        return merged
    except Exception as e:
        logger.warning("DietTrackerStep: ticket fetch failed: %s", e)
        return []


def _render_tickets_for_agent(tickets: List[Any]) -> str:
    """Render diet-relevant tickets into a block the agent reads alongside pods."""
    if not tickets:
        return "(no new diet-related tickets)"
    blocks: List[str] = []
    for t in tickets:
        responded_iso = t.responded_at.astimezone(timezone.utc).isoformat() if t.responded_at else ""
        created_iso = t.created_at.astimezone(timezone.utc).isoformat() if t.created_at else ""
        try:
            local_time = utc_to_local(t.responded_at or t.created_at).strftime("%Y-%m-%d %I:%M %p").lstrip("0")
        except Exception:
            local_time = ""
        effects = t.status_effect or []
        state = t.state or ""
        action = (t.user_action or "").strip()
        user_text = (t.user_text or "").strip()
        blocks.append(
            f"--- ticket_id: {t.ticket_id} | effects: {effects} | state: {state} "
            f"| at_local: {local_time} | responded_utc: {responded_iso or '(not responded)'} "
            f"| created_utc: {created_iso} ---\n"
            f"title: {t.title or ''}\n"
            f"message: {t.message or ''}\n"
            f"user_action: {action}\n"
            + (f"user_text: {user_text}\n" if user_text else "")
        )
    return "\n\n".join(blocks)


def _write_markdown_view(ctx: StepContext, log: Dict[str, Any]) -> None:
    """Render the diet log as readable markdown alongside the JSON.

    Agents that prefer text context (chat_gate, recap-style consumers) can
    inject `resource_diet_log_today` (the .md lands at the same stem).
    """
    lines: List[str] = []
    lines.append(f"# Diet Log — {log.get('date_local','?')}")
    lines.append("")
    summary = str(log.get("summary") or "").strip()
    if summary:
        lines.append(f"**Summary:** {summary}")
        lines.append("")
    items = log.get("items") or []
    if items:
        lines.append("## Items")
        for item in items:
            when = item.get("eaten_at_local") or "?"
            food = item.get("food") or "?"
            qty = item.get("quantity") or ""
            kind = item.get("meal_kind") or ""
            qty_str = f" — {qty}" if qty else ""
            kind_str = f" [{kind}]" if kind else ""
            lines.append(f"- `{when}` {food}{qty_str}{kind_str}")
            refs = item.get("source_pod_ids") or []
            if refs:
                lines.append(f"    - sources: {', '.join(refs)}")
    else:
        lines.append("_No items logged yet today._")
    lines.append("")
    open_qs = log.get("open_questions") or []
    if open_qs:
        lines.append("## Open Questions")
        for q in open_qs:
            lines.append(f"- {q}")
        lines.append("")
    updated = log.get("updated_at_utc") or ""
    if updated:
        lines.append(f"_updated: {updated}_")
    md_text = "\n".join(lines)

    # Write via resources_dir directly — ctx.write_resource is JSON-only.
    md_path = ctx.resources_dir / _OUTPUT_MD_FILENAME
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_text, encoding="utf-8")

    # Update resource_manager cache so consumers see the new text immediately.
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        rm = getattr(DI, "resource_manager", None)
        if rm is not None:
            rm.update_resource(_OUTPUT_MD_FILENAME.rsplit(".", 1)[0], md_text, persist=False)
    except Exception as e:
        logger.debug("diet_tracker md cache update failed: %s", e, exc_info=True)


def _render_pods_for_agent(pods: List[Pod]) -> str:
    """Format pods as a readable block the agent reads verbatim.

    Each entry: header line with pod_id/tags/scope/time + the pod's body.
    Body is already entity-resolved by the pod pipeline, so pronouns are
    disambiguated (e.g. "i (Jukka) had yogurt").

    The header exposes both a local date and an ISO UTC timestamp so the
    agent can combine a per-message ``- [4:18 PM] ...`` line with the
    pod's date to produce a precise eaten_at_local / eaten_at_utc.
    """
    if not pods:
        return "(no new food pods)"
    blocks: List[str] = []
    for pod in pods:
        created_iso = pod.created_at.astimezone(timezone.utc).isoformat() if pod.created_at else ""
        try:
            local_full = utc_to_local(pod.created_at).strftime("%Y-%m-%d %I:%M %p").lstrip("0") if pod.created_at else ""
            local_date = utc_to_local(pod.created_at).strftime("%Y-%m-%d") if pod.created_at else ""
        except Exception:
            local_full = ""
            local_date = ""
        tags = ", ".join(pod.tags or [])
        blocks.append(
            f"--- pod_id: {pod.pod_id} | tags: [{tags}] | scope: {pod.scope_id or '?'} "
            f"| at_local: {local_full or '?'} | at_utc: {created_iso or '?'} "
            f"| pod_date_local: {local_date or '?'} ---\n"
            f"one_liner: {pod.one_liner}\n"
            f"body (per-message times inside are on pod_date_local unless noted):\n"
            f"{pod.body or '(empty body)'}"
        )
    return "\n\n".join(blocks)
