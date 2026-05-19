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


def build_meal_proposer_context() -> Dict[str, str]:
    """Assemble the context dict for one meal_proposer run."""
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
    }


# ── section builders ─────────────────────────────────────────────────────


def _build_inventory_snapshot() -> str:
    """Read resource_grocery_inventory.json — empty in Phase 1a.

    Phase 1b wires the chat handlers for "I did groceries" / "I had X" to
    update this state file. For now, return an empty stub and let the
    proposer propose from-scratch shopping for whatever it wants.
    """
    path = get_repo_root() / "resources" / "subconscious" / "resource_grocery_inventory.json"
    if not path.is_file():
        return "(empty — inventory tracking not yet active; the proposer should assume nothing is on hand and emit a shopping_run for any proposal needing ingredients)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[meal_context] inventory parse failed: %s", e)
        return "(error reading inventory)"

    items = data.get("items") or []
    if not items:
        return "(inventory currently empty)"

    lines = ["Current inventory:"]
    for item in items:
        name = item.get("name", "?")
        category = item.get("category", "?")
        decay = item.get("decay_at", "?")
        lines.append(f"- {name} ({category}) — decays {decay}")
    return "\n".join(lines)


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
