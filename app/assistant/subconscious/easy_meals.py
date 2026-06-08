"""Easy-meals registry — the structured "go-to dishes we're OK with" layer.

A small, UI-managed list that sits ALONGSIDE the hand-edited markdown registry
(resource_household_food_registry.md), per the chosen design: don't migrate the
markdown, add a structured layer on top. Each entry carries a cadence
(``max_days`` = "we're OK with this at most every N days") + free-text notes.

"Days since last" is NOT stored — it's DERIVED from recent plan/intention history
so it self-updates as plans run (until the nightly "what did you actually eat?"
confirm exists, planned-history is the proxy).

Storage: resources/subconscious/resource_easy_meals.json (gitignored — food PII).
Shape:
    {
      "schema_version": 1,
      "last_updated_utc": "...",
      "meals": [
        {"id": "...", "name": "Frozen pizza", "max_days": 7, "notes": "kids love"}
      ]
    }
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_EASY_MEALS_REL = "resources/subconscious/resource_easy_meals.json"
_REGISTRY_REL = "resources/instructions/resource_household_food_registry.md"
_DEFAULT_MAX_DAYS = 7
_HISTORY_LOOKBACK_DAYS = 90

# Words dropped when token-matching an easy-meal name against a planned dish, so
# "Frozen pizza" matches "Frozen pepperoni pizza" and "Salmon" matches "Baked
# salmon with broccoli".
_STOPWORDS = {
    "with", "and", "the", "a", "an", "of", "in", "on", "over", "for", "to",
    "&", "or", "plus", "side", "sides",
}


# ── load / save ───────────────────────────────────────────────────────────


def load_easy_meals() -> Dict[str, Any]:
    """Load the easy-meals registry. On first run (file absent), seed it from the
    markdown registry's 'Comfort food subset'. Fails LOUD on a corrupt file — a
    silently-emptied list would hide a real problem and lose the user's data."""
    path = get_repo_root() / _EASY_MEALS_REL
    if not path.is_file():
        seeded = {"schema_version": 1, "last_updated_utc": None, "meals": _seed_from_registry()}
        save_easy_meals(seeded)
        return seeded
    return json.loads(path.read_text(encoding="utf-8"))


def save_easy_meals(data: Dict[str, Any]) -> None:
    data["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
    path = get_repo_root() / _EASY_MEALS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_from_registry() -> List[Dict[str, Any]]:
    """Best-effort seed from the markdown registry's 'Comfort food subset' bullets.
    Convenience only — if the section can't be parsed, start empty (logged)."""
    path = get_repo_root() / _REGISTRY_REL
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        # Grab bullets under the "Comfort food subset" header until the next header.
        m = re.search(r"Comfort food subset.*?\n(.*?)(?:\n#{2,3}\s|\Z)", text, re.DOTALL)
        if not m:
            return []
        meals: List[Dict[str, Any]] = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            raw = line[2:].strip()
            note_m = re.search(r"\(([^)]*)\)", raw)
            notes = note_m.group(1).strip() if note_m else ""
            name = re.sub(r"\s*\([^)]*\)", "", raw).strip()
            if name:
                meals.append({
                    "id": uuid.uuid4().hex[:12],
                    "name": name,
                    "max_days": _DEFAULT_MAX_DAYS,
                    "notes": notes,
                })
        return meals
    except Exception as e:
        logger.warning("[easy_meals] registry seed parse failed: %s", e)
        return []


# ── mutations ─────────────────────────────────────────────────────────────


def add_easy_meal(name: str, max_days: int, notes: str = "") -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        return {"status": "empty_name"}
    data = load_easy_meals()
    data["meals"].append({
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "max_days": max(1, int(max_days or _DEFAULT_MAX_DAYS)),
        "notes": (notes or "").strip(),
    })
    save_easy_meals(data)
    return {"status": "added", "name": name}


def update_easy_meal(meal_id: str, *, max_days: Optional[int] = None,
                     notes: Optional[str] = None) -> Dict[str, Any]:
    data = load_easy_meals()
    target = next((m for m in data["meals"] if m.get("id") == meal_id), None)
    if target is None:
        return {"status": "not_found"}
    if max_days is not None:
        target["max_days"] = max(1, int(max_days))
    if notes is not None:
        target["notes"] = notes.strip()
    save_easy_meals(data)
    return {"status": "updated", "name": target.get("name")}


def remove_easy_meal(meal_id: str) -> Dict[str, Any]:
    data = load_easy_meals()
    before = len(data["meals"])
    data["meals"] = [m for m in data["meals"] if m.get("id") != meal_id]
    if len(data["meals"]) == before:
        return {"status": "not_found"}
    save_easy_meals(data)
    return {"status": "removed"}


# ── "days since last" derivation (from planned history) ────────────────────


def _load_dish_history() -> List[Tuple[str, str]]:
    """(date_iso, dish) pairs from recent weekly plans + daily intention.meal pods.
    Planned history is the proxy for 'last served' until a confirm loop exists."""
    from datetime import timedelta
    from app.assistant.pod_store.pod_store import PodStore
    store = PodStore()
    out: List[Tuple[str, str]] = []
    try:
        for pod in store.query(kind="plan.weekly_meals", limit=16):
            for slot in (pod.metadata or {}).get("slots") or []:
                d, dish = slot.get("date"), slot.get("dish")
                if d and dish:
                    out.append((str(d)[:10], str(dish)))
    except Exception as e:
        logger.warning("[easy_meals] plan history fetch failed: %s", e)
    try:
        since = datetime.now(timezone.utc) - timedelta(days=_HISTORY_LOOKBACK_DAYS)
        for pod in store.query(kind="intention.meal", since_utc=since, limit=300):
            md = pod.metadata or {}
            d, dish = md.get("date"), md.get("dish")
            if d and dish:
                out.append((str(d)[:10], str(dish)))
    except Exception as e:
        logger.warning("[easy_meals] intention history fetch failed: %s", e)
    return out


def _significant_tokens(name: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t and t not in _STOPWORDS]


def _dish_matches(dish: str, easy_name: str) -> bool:
    """A planned dish counts as 'this easy meal' if all the easy-meal's significant
    tokens appear in the dish text. Falls back to substring for one-word names."""
    dish_l = (dish or "").lower()
    toks = _significant_tokens(easy_name)
    if not toks:
        return (easy_name or "").lower().strip() in dish_l
    return all(t in dish_l for t in toks)


def build_easy_meals_rotation() -> List[Dict[str, Any]]:
    """Each easy meal annotated with last-served + cadence status. Sorted
    most-overdue first (due before resting). Shared by the UI and the planner."""
    data = load_easy_meals()
    history = _load_dish_history()
    today = datetime.now(timezone.utc).date()
    today_iso = today.isoformat()
    rows: List[Dict[str, Any]] = []
    for m in data.get("meals") or []:
        name = m.get("name", "")
        max_days = int(m.get("max_days") or _DEFAULT_MAX_DAYS)
        # Only PAST occurrences count as "last served" — a dish on next week's
        # plan hasn't been eaten yet, so future dates must not mark it "resting".
        dates = [d for (d, dish) in history if d <= today_iso and _dish_matches(dish, name)]
        last = max(dates) if dates else None
        days_since: Optional[int] = None
        if last:
            try:
                days_since = (today - datetime.fromisoformat(last).date()).days
            except Exception:
                days_since = None
        if days_since is None:
            status = "never"
        elif days_since >= max_days:
            status = "due"
        else:
            status = "resting"
        rows.append({
            "id": m.get("id"),
            "name": name,
            "max_days": max_days,
            "notes": m.get("notes", ""),
            "last_served": last,
            "days_since": days_since,
            "status": status,
        })
    order = {"due": 0, "never": 1, "resting": 2}
    rows.sort(key=lambda r: (order.get(r["status"], 3),
                             -(r["days_since"] or 0) if r["status"] != "resting" else (r["days_since"] or 0)))
    return rows


def render_easy_meals_for_planner() -> str:
    """Text block for the planner/distiller context: which go-to meals are DUE to
    rotate back in vs RESTING (served too recently). Empty list -> a short note."""
    rows = build_easy_meals_rotation()
    if not rows:
        return "(no easy-meals registry yet — user can add go-to dishes at /meals/easy-meals)"
    lines = [
        "Household 'easy meals' rotation (go-to dishes the family is OK with). "
        "Prefer DUE dishes; avoid proposing RESTING ones (served too recently):"
    ]
    for r in rows:
        if r["status"] == "never":
            when = "not in recent history — fine to use"
        elif r["status"] == "due":
            when = f"last ~{r['days_since']}d ago, cadence {r['max_days']}d — DUE"
        else:
            when = f"last ~{r['days_since']}d ago, cadence {r['max_days']}d — RESTING"
        note = f" ({r['notes']})" if r["notes"] else ""
        lines.append(f"- {r['name']}{note}: {when}")
    return "\n".join(lines)
