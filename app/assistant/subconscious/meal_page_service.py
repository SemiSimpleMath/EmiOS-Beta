"""Data + actions for the /meals page.

Three responsibilities:
1. Load the latest weekly plan + this week's shopping context for rendering
2. Send the weekly meal plan email to a recipient
3. Mint a delivery.email audit pod recording each send

The route layer (app/routes/meals.py) is a thin wrapper around this.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.subconscious.meal_email_renderer import (
    consolidate_intention_shopping,
)
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def load_latest_weekly_plan_pod() -> Optional[Pod]:
    """Most recent plan.weekly_meals pod (across all weeks). Returns None
    if no plan exists yet."""
    store = PodStore()
    results = store.query(kind="plan.weekly_meals", limit=1)
    return results[0] if results else None


def monday_of(d: date) -> date:
    """Monday of the ISO week containing ``d``. ``d.weekday()`` returns
    0 for Monday, so this works without further casing."""
    return d - timedelta(days=d.weekday())


def load_plan_pod_for_week(week_start: str) -> Optional[Pod]:
    """Most recent plan pod whose metadata.week_start_date matches.

    The planner may have run multiple times for the same week; the
    newest produced_at_utc wins (matches PodStore ordering)."""
    store = PodStore()
    # Pull a generous window so we don't miss a plan written a few weeks
    # before the user navigates back to it.
    candidates = store.query(kind="plan.weekly_meals", since="365d", limit=200)
    for p in candidates:
        if str((p.metadata or {}).get("week_start_date") or "") == week_start:
            return p
    return None


def list_existing_plan_week_starts() -> List[str]:
    """Sorted set of every week_start_date that has at least one plan
    pod. Used to drive the prev/next navigation chrome — the arrow is
    live to weeks with plans, otherwise points at the empty state with
    a Generate button."""
    store = PodStore()
    candidates = store.query(kind="plan.weekly_meals", since="365d", limit=500)
    starts = {
        str((p.metadata or {}).get("week_start_date") or "")
        for p in candidates
    }
    return sorted(s for s in starts if s)


def load_recent_intention_shopping_pods(*, days: int = 14) -> List[Pod]:
    """intention.shopping pods from the past `days` — the ad-hoc additions
    daily_meal_proposer mints when a meal needs items outside the weekly
    list."""
    store = PodStore()
    return store.query(kind="intention.shopping", since=f"{days}d", limit=50)


def fetch_weekly_shopping_doc_body() -> Optional[str]:
    """Fetch the agent's weekly shopping list Google Doc body, if it
    exists. Returns None when there's no doc state or the fetch fails."""
    from app.assistant.subconscious.meal_context_builder import load_weekly_doc_state

    state = load_weekly_doc_state()
    doc_id = state.get("doc_id")
    if not doc_id:
        return None

    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage

        cls = DI.tool_registry.get_tool_class("get_google_doc")
        if cls is None:
            logger.warning("[meal_page_service] get_google_doc tool unavailable")
            return None
        tool = cls()
        tm = ToolMessage(
            tool_name="get_google_doc",
            tool_data={
                "arguments": {
                    "document_id": doc_id,
                    "include_body": True,
                    "max_chars": 50000,
                },
            },
        )
        result = tool.execute(tm)
        data = getattr(result, "data", None) or {}
        body = data.get("body")
        return str(body).strip() if body else None
    except Exception as e:
        logger.warning("[meal_page_service] fetch weekly doc failed: %s", e)
        return None


def build_shopping_text(*, doc_body: Optional[str], ad_hoc_pods: List[Pod]) -> str:
    """Compose the shopping section: weekly doc body (canonical) plus any
    ad-hoc items added by daily proposer since the doc was last refreshed."""
    sections: List[str] = []
    if doc_body:
        sections.append("From this week's planned list:")
        sections.append(doc_body)

    ad_hoc_items = consolidate_intention_shopping(ad_hoc_pods)
    if ad_hoc_items:
        if sections:
            sections.append("")
        sections.append("Ad-hoc additions (added by daily proposer):")
        for it in ad_hoc_items:
            sections.append(f"- {it}")

    return "\n".join(sections).strip()


def build_page_view_model(week_start: Optional[str] = None) -> Dict[str, Any]:
    """Everything the /meals template needs to render.

    Selection precedence for which week to display:
      1. Explicit ``week_start`` query param, if given.
      2. Latest plan pod that exists (so first-time visitors land on
         real content), if any.
      3. Monday of today (empty state with a Generate button).
    """
    from app.assistant.subconscious.feedback_service import fetch_comments_for

    today = date.today()
    if week_start:
        viewing_week = _parse_week_start(week_start) or monday_of(today)
    else:
        latest = load_latest_weekly_plan_pod()
        if latest is not None:
            viewing_week = (
                _parse_week_start(str((latest.metadata or {}).get("week_start_date") or ""))
                or monday_of(today)
            )
        else:
            viewing_week = monday_of(today)

    viewing_week_iso = viewing_week.isoformat()
    plan_pod = load_plan_pod_for_week(viewing_week_iso)

    doc_body = fetch_weekly_shopping_doc_body()
    ad_hoc_pods = load_recent_intention_shopping_pods()
    shopping_text = build_shopping_text(doc_body=doc_body, ad_hoc_pods=ad_hoc_pods)

    plan_view: Optional[Dict[str, Any]] = None
    plan_comments: list = []
    if plan_pod is not None:
        meta = plan_pod.metadata or {}
        plan_view = {
            "pod_id": plan_pod.pod_id,
            "week_start_date": meta.get("week_start_date") or "",
            "slots": list(meta.get("slots") or []),
            "produced_at_utc": meta.get("produced_at_utc") or "",
            "theme": _extract_theme(plan_pod.body or ""),
        }
        plan_comments = fetch_comments_for(plan_pod.pod_id, limit=20)

    plan_comments_by_scope: Dict[str, list] = {}
    plan_comments_section: list = []
    for c in plan_comments:
        scope = c.get("target_scope")
        if scope:
            plan_comments_by_scope.setdefault(str(scope), []).append(c)
        else:
            plan_comments_section.append(c)

    nav = _build_week_nav(viewing_week, today=today)

    return {
        "plan": plan_view,
        "plan_comments_by_scope": plan_comments_by_scope,
        "plan_comments_section": plan_comments_section,
        "shopping_text": shopping_text,
        "weekly_doc_present": bool(doc_body),
        "ad_hoc_count": len(consolidate_intention_shopping(ad_hoc_pods)),
        "viewing_week": viewing_week_iso,
        "nav": nav,
    }


def build_inventory_view_model() -> Dict[str, Any]:
    """Everything the /meals/inventory editor needs: active items with a
    computed freshness (days until decay), sorted soonest-to-expire first,
    plus the category vocabulary for the add form."""
    from app.assistant.subconscious.grocery_inventory import (
        load_inventory,
        CATEGORY_DECAY_DAYS,
    )

    inv = load_inventory()
    now = datetime.now(timezone.utc)
    rows: List[Dict[str, Any]] = []
    for it in (inv.get("items") or []):
        if it.get("consumed_at_utc"):
            continue
        days_left: Optional[int] = None
        try:
            decay = datetime.fromisoformat((it.get("decay_at_utc") or "").replace("Z", "+00:00"))
            days_left = (decay - now).days
        except Exception:
            pass
        if days_left is None:
            freshness = "unknown"
        elif days_left < 0:
            freshness = "expired"
        elif days_left <= 2:
            freshness = "soon"
        else:
            freshness = "fresh"
        rows.append({
            "id": it.get("id"),
            "name": it.get("name"),
            "category": it.get("category") or "unknown",
            "days_left": days_left,
            "decay_date": (it.get("decay_at_utc") or "")[:10],
            "acquired_date": (it.get("acquired_at_utc") or "")[:10],
            "freshness": freshness,
        })
    # Soonest-to-expire first (None/unknown sink to the bottom).
    rows.sort(key=lambda r: r["days_left"] if r["days_left"] is not None else 9999)

    return {
        "items": rows,
        "count": len(rows),
        "categories": sorted(CATEGORY_DECAY_DAYS.keys()),
        "last_updated": (inv.get("last_updated_utc") or "")[:19].replace("T", " "),
    }


def build_easy_meals_view_model() -> Dict[str, Any]:
    """Everything the /meals/easy-meals editor needs: each go-to dish with its
    cadence + derived 'days since last' + rotation status (due / resting / never),
    most-overdue first."""
    from app.assistant.subconscious.easy_meals import (
        build_easy_meals_rotation,
        load_easy_meals,
    )
    rows = build_easy_meals_rotation()
    data = load_easy_meals()
    return {
        "meals": rows,
        "count": len(rows),
        "default_max_days": 7,
        "last_updated": (data.get("last_updated_utc") or "")[:19].replace("T", " "),
    }


def _parse_week_start(s: str) -> Optional[date]:
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    # Snap to Monday so a misuse like ?week=2026-05-26 still resolves
    # to the same week the planner would have minted under.
    return monday_of(d)


def _build_week_nav(viewing_week: date, *, today: date) -> Dict[str, Any]:
    """Compute the prev/next week ISO dates plus convenience flags for
    rendering navigation in the template."""
    prev_week = viewing_week - timedelta(days=7)
    next_week = viewing_week + timedelta(days=7)
    today_week = monday_of(today)
    existing_weeks = set(list_existing_plan_week_starts())
    return {
        "viewing_week_iso": viewing_week.isoformat(),
        "prev_week_iso": prev_week.isoformat(),
        "next_week_iso": next_week.isoformat(),
        "today_week_iso": today_week.isoformat(),
        "is_current_week": viewing_week == today_week,
        "prev_has_plan": prev_week.isoformat() in existing_weeks,
        "next_has_plan": next_week.isoformat() in existing_weeks,
    }


def generate_plan_for_week(week_start: str) -> Dict[str, Any]:
    """Run the weekly meal planning chain for ``week_start`` and persist.

    Returns ``{status: ok|error, week_start, plan_pod_id?, message?}``. Thin wrapper over
    the shared ``run_weekly_meal_planning_chain`` — the SAME path the Sunday cron and the
    CLI use, so the button produces an identical, fully-distilled plan. The chain fails
    loud; here (a user-facing route) we convert that to a status dict for the UI.
    """
    parsed = _parse_week_start(week_start)
    if parsed is None:
        return {"status": "error", "message": f"Invalid week_start: {week_start!r}"}
    week_iso = parsed.isoformat()

    from app.assistant.subconscious.weekly_meal_planning_runner import (
        run_weekly_meal_planning_chain,
    )
    try:
        summary = run_weekly_meal_planning_chain(week_start=week_iso, persist=True)
    except Exception as e:
        logger.exception("[meal_page_service] weekly planning chain failed")
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    return {
        "status": "ok",
        "week_start": week_iso,
        "plan_pod_id": summary.get("plan_pod_id") or "",
        "summary": summary,
    }


def _extract_theme(body: str) -> str:
    marker = "**Theme:**"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(marker):
            return line[len(marker):].strip()
    return ""
