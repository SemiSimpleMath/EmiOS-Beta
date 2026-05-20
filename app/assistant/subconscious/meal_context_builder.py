"""Build the injected context dict for the meal_proposer agent.

Reuses some helpers from the noticer's context_builder (diet log, calendar,
family roster, KG household digests) and adds meal-specific assembly:
- addressable_concerns: concerns_register filtered to those routed to
  meal_proposer
- inventory_snapshot: grocery inventory (stub for 1a — empty list)
- recipes_house: from resource_meal_proposer_house_rules (just a stub
  reference; the agent reads the actual recipes from house_rules in system
  context)
- dietary_context: KG-derived medical + dietary patterns for Jukka
- fast_food_count_7d: scan diet_log for delivery/restaurant entries
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.subconscious.context_builder import (
    _build_calendar_today_tomorrow,
    _build_calendar_week_summary,
    _build_diet_log,
    _build_family_roster,
    _build_kg_household_digests,
    _resolve_household_members,
    _NO_DATA_FMT,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


def build_weekly_meal_planner_context() -> Dict[str, str]:
    """Context for weekly_meal_planner (strategic, full-week)."""
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "week_start_date": _compute_week_start_date(now_local),
        "diet_log_7d": _build_diet_log(),
        "inventory_snapshot": _build_inventory_snapshot(),
        "recipes_house": _build_recipes_house_reference(),
        "dietary_context": _build_dietary_context(household_members),
        "food_calendar": _build_food_calendar(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "addressable_concerns": _build_addressable_concerns(),
        "fast_food_count_7d": _build_fast_food_count(),
        "ralphs_standing_list": _build_ralphs_standing_list(),
        "agent_weekly_list_state": _build_agent_weekly_list_state(),
    }


def build_daily_meal_proposer_context() -> Dict[str, str]:
    """Context for daily_meal_proposer (tactical, 24-48h)."""
    now_local = get_local_time()
    household_members = _resolve_household_members()

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "diet_log_7d": _build_diet_log(),
        "inventory_snapshot": _build_inventory_snapshot(),
        "recipes_house": _build_recipes_house_reference(),
        "dietary_context": _build_dietary_context(household_members),
        "food_calendar": _build_food_calendar(now_local=now_local),
        "general_calendar": _build_calendar_today_tomorrow(now_local=now_local),
        "family_roster": _build_family_roster(household_members),
        "addressable_concerns": _build_addressable_concerns(),
        "fast_food_count_7d": _build_fast_food_count(),
        "latest_weekly_plan": _build_latest_weekly_plan(),
        "ralphs_standing_list": _build_ralphs_standing_list(),
    }


# ── section builders ─────────────────────────────────────────────────────


def _build_inventory_snapshot() -> str:
    """Render current grocery inventory via grocery_inventory.render_inventory_summary.

    State is populated by run_grocery_sync (Phase 1b): the scanner agent
    detects "I did groceries" / "I had the salmon" / "we ran out of X" in
    recent chat and applies the corresponding intention.shopping or
    intention.meal pod's items to inventory, plus daily decay.

    Returns a markdown summary grouped by category with USE-SOON callouts.
    """
    from app.assistant.subconscious.grocery_inventory import render_inventory_summary
    try:
        return render_inventory_summary()
    except Exception as e:
        logger.warning("[meal_context] inventory render failed: %s", e)
        return "(error reading inventory)"


def _build_recipes_house_reference() -> str:
    """The actual recipe list lives in the system_context_items
    (resource_meal_proposer_house_rules). We just point to it here."""
    return "(see resource_meal_proposer_house_rules in your system context for the full recipe vocabulary this family cooks regularly)"


def _build_dietary_context(household_members: List[str]) -> str:
    """KG-derived dietary context for Jukka + family.

    For Phase 1a, return Jukka's KG entity card description filtered for
    health/dietary mentions. Phase 1c would use ask_kg with a focused query.
    """
    try:
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        from app.models.base import get_session

        session = get_session()
        try:
            # Jukka is the primary user — most household dietary constraints
            # belong to him (per the existing KG card structure).
            primary = household_members[0] if household_members else "Jukka"
            row = (
                session.query(Node)
                .filter(Node.label == primary)
                .filter(Node.node_type == "Entity")
                .one_or_none()
            )
        finally:
            session.close()

        if row is None:
            return f"(no KG entity card for {primary})"
        description = (row.description or "").strip()
        if not description:
            return f"(node exists for {primary}, no description)"
        # Filter to health/dietary-relevant sentences.
        keywords = (
            "diet", "eat", "food", "meal", "fast", "calorie", "weight",
            "diabet", "gerd", "allerg", "lactose", "gluten", "vegetar",
            "vegan", "pescatarian", "drink", "alcohol", "caffeine",
            "intermittent", "carb", "protein", "sugar", "salt",
        )
        sentences = description.split(". ")
        relevant = [s.strip() for s in sentences if any(k in s.lower() for k in keywords)]
        if not relevant:
            return "(no dietary signals in KG card; proposer should default to neutral, family-frequent meals)"
        return f"Dietary context for {primary}:\n" + "\n".join(f"- {s}." for s in relevant)
    except Exception as e:
        logger.warning("[meal_context] dietary_context build failed: %s", e)
        return "(error reading dietary context)"


def _build_food_calendar(*, now_local: datetime) -> str:
    """Already-planned meals on the Food calendar, next 7 days."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind="food calendar")
        tool = cls()
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
        tm = ToolMessage(
            tool_name="get_calendar_events",
            tool_data={
                "arguments": {
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "calendar_names": ["Food"],
                    "single_events": True,
                },
            },
        )
        result = tool.execute(tm)
    except Exception as e:
        logger.warning("[meal_context] food_calendar fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="food calendar")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return "(Food calendar is empty for the next 7 days — wide open for proposals)"
    return content


def _build_addressable_concerns() -> str:
    """Read concerns_register, filter to concerns where addressable_by
    includes meal_proposer."""
    path = get_repo_root() / "resources" / "subconscious" / "resource_concerns_register.json"
    if not path.is_file():
        return "(no concerns_register yet)"
    try:
        register = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] concerns_register parse failed: %s", e)
        return "(error reading concerns_register)"

    matched: List[Dict[str, Any]] = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket, []) or []:
            if "meal_proposer" in (c.get("addressable_by") or []):
                matched.append(c)

    if not matched:
        return "(no concerns currently routed to meal_proposer)"

    lines: List[str] = []
    for c in matched:
        lines.append(
            f"[{c.get('severity', '?')}/{c.get('horizon', '?')}] "
            f"{c.get('concern_id', '?')} — {c.get('title', '(untitled)')}"
        )
        lines.append(f"  subject: {c.get('subject') or 'household'}")
        lines.append(f"  kind: {c.get('kind', '?')}")
        notes = (c.get("notes") or "").strip()
        if notes:
            lines.append(f"  notes: {notes[:400]}")
        # Also include co-addressable agents so the proposer knows it's
        # not the only one being asked to address this.
        co = [a for a in (c.get("addressable_by") or []) if a != "meal_proposer"]
        if co:
            lines.append(f"  co-addressable: {', '.join(co)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_fast_food_count() -> str:
    """Scan diet_log for delivery/restaurant entries in last 7 days.

    For Phase 1a, this is approximate — reads the diet_log markdown looking
    for 'delivery' or 'restaurant' tags. Better integration once diet_log
    has structured source labels.
    """
    md_path = get_repo_root() / "resources" / "dayflow_pipeline_outputs" / "resource_diet_log_today_text.md"
    if not md_path.is_file():
        return "0 (no diet log to scan)"
    try:
        text = md_path.read_text(encoding="utf-8").lower()
    except Exception:
        return "0 (error)"

    # Crude but useful for v0
    keywords = ("delivery", "doordash", "uber eats", "restaurant", "fast food", "mcd", "takeout")
    count = sum(1 for k in keywords if k in text)
    return f"{count} (rough scan; structured fast-food tagging is Phase 1b)"


# ── Phase 1c.1: external lists (Ralphs standing + agent's weekly) ─────────


_EXTERNAL_LISTS_REL = "resources/subconscious/resource_meal_proposer_external_lists.json"
_WEEKLY_DOC_STATE_REL = "resources/subconscious/resource_meal_proposer_weekly_doc_state.json"


def _load_external_lists() -> Dict[str, Any]:
    path = get_repo_root() / _EXTERNAL_LISTS_REL
    if not path.is_file():
        return {"lists": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] external_lists parse failed: %s", e)
        return {"lists": []}


def load_weekly_doc_state() -> Dict[str, Any]:
    """Public — also used by meal_persist."""
    path = get_repo_root() / _WEEKLY_DOC_STATE_REL
    if not path.is_file():
        return {"doc_id": None, "doc_title": None, "last_built_week_start": None, "last_edit_at_utc": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] weekly_doc_state parse failed: %s", e)
        return {"doc_id": None, "doc_title": None, "last_built_week_start": None, "last_edit_at_utc": None}


def save_weekly_doc_state(state: Dict[str, Any]) -> None:
    path = get_repo_root() / _WEEKLY_DOC_STATE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _build_ralphs_standing_list() -> str:
    """Read the Ralphs doc (Jukka's standing list of staples) via
    get_google_doc tool. Read-only — the proposer never edits this list."""
    lists = _load_external_lists().get("lists") or []
    ralphs = next((l for l in lists if (l.get("name") or "").lower() == "ralphs"), None)
    if not ralphs:
        return "(no Ralphs list configured in resource_meal_proposer_external_lists.json)"
    doc_id = ralphs.get("doc_id")
    if not doc_id or "REPLACE_WITH_REAL" in str(doc_id):
        return "(Ralphs doc_id not set in config; placeholder still in place)"

    body = _fetch_google_doc_body(doc_id)
    if body is None:
        return f"(Ralphs doc {doc_id} could not be read — google auth issue?)"

    return (
        "Jukka's standing Ralphs list (READ-ONLY, these items are staples he "
        "always buys — treat them as 'about to be acquired' and DON'T include "
        "them in your weekly shopping list or shopping_run):\n\n"
        f"{body.strip() or '(doc is empty)'}"
    )


def _build_agent_weekly_list_state() -> str:
    """Read the agent's own per-week shopping list (if it exists) via
    get_google_doc tool. Used so weekly_plan ticks can see the current state
    before replacing it, and daily ticks can read what's already there."""
    state = load_weekly_doc_state()
    doc_id = state.get("doc_id")
    last_built = state.get("last_built_week_start")
    if not doc_id:
        return (
            "(No agent weekly list doc yet — this is bootstrap. On the next "
            "weekly_plan tick, set weekly_shopping_list.action='create' and "
            "provide body_markdown + week_start_date; persist will create the "
            "doc and store its id.)"
        )

    body = _fetch_google_doc_body(doc_id)
    if body is None:
        return f"(agent weekly list doc {doc_id} could not be read)"

    return (
        f"Agent's current weekly shopping list doc (doc_id={doc_id}, "
        f"last_built_week_start={last_built}):\n\n"
        f"{body.strip() or '(doc is empty)'}"
    )


def _fetch_google_doc_body(doc_id: str, *, max_chars: int = 8000) -> Optional[str]:
    """Single shared fetcher. Uses the get_google_doc tool via ToolRegistry."""
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage

        cls = DI.tool_registry.get_tool_class("get_google_doc")
        if cls is None:
            logger.warning("[meal_context] get_google_doc tool unavailable")
            return None
        tool = cls()
        tm = ToolMessage(
            tool_name="get_google_doc",
            tool_data={
                "arguments": {
                    "document_id": doc_id,
                    "include_body": True,
                    "max_chars": max_chars,
                },
            },
        )
        result = tool.execute(tm)
        data = getattr(result, "data", None) or {}
        body = data.get("body")
        if body is None:
            return None
        return str(body)
    except Exception as e:
        logger.warning("[meal_context] fetch google doc %s failed: %s", doc_id, e)
        return None


# ── Phase 1c.2: helpers for the weekly/daily split ────────────────────────


def _compute_week_start_date(now_local: datetime) -> str:
    """ISO date of the Monday on or before now_local."""
    days_since_monday = now_local.weekday()  # Monday = 0
    monday = (now_local - timedelta(days=days_since_monday)).date()
    return monday.isoformat()


def _build_latest_weekly_plan() -> str:
    """Read the most recent plan.weekly_meals pod (from weekly_meal_planner).
    The daily proposer reads this to know which slots are anchor/leftover/flex
    for the next 24-48h. Falls back gracefully when no plan exists."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        since = datetime.now(timezone.utc) - timedelta(days=10)
        results = store.query(kind="plan.weekly_meals", since_utc=since, limit=5)
    except Exception as e:
        logger.warning("[meal_context] weekly plan fetch failed: %s", e)
        return "(no weekly plan available — propose from inventory + recipes + concerns; set fills_weekly_plan_slot=null)"

    if not results:
        return "(no weekly plan pod exists yet — run weekly_meal_planner first; propose from inventory + recipes + concerns; set fills_weekly_plan_slot=null)"

    latest = results[0]
    return f"{latest.one_liner}\n\n{latest.body}"
