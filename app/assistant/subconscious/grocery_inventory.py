"""Grocery inventory state — load, mutate (acquire / consume / decay), save.

Used by the meal_proposer (reads current state) and the grocery sync runner
(applies user-confirmed shopping trips + meal consumption + daily decay).

Storage: resources/subconscious/resource_grocery_inventory.json (gitignored).
Shape:
    {
      "schema_version": 1,
      "last_updated_utc": "...",
      "items": [
        {
          "name": "salmon fillet",
          "category": "fresh_fish",
          "acquired_at_utc": "2026-05-19T...",
          "decay_at_utc":    "2026-05-22T...",
          "source_intention_id": "datapod:intention.shopping:...",
          "consumed_at_utc": null
        }
      ],
      "history": [...]   # items that decayed or were consumed
    }

Categories + default decay (days from acquired_at):
    fresh_fish     3
    fresh_meat     4
    fresh_produce  7
    dairy         10
    bread          5
    frozen        60
    pantry       180
    unknown        7   (conservative default)
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


_INVENTORY_REL = "resources/subconscious/resource_grocery_inventory.json"


# Per-category decay defaults (days). Conservative side — the proposer can
# read decay_at and weight toward soon-to-expire.
CATEGORY_DECAY_DAYS = {
    "fresh_fish": 3,
    "fresh_meat": 4,
    "fresh_produce": 7,
    "dairy": 10,
    "bread": 5,
    "frozen": 60,
    "pantry": 180,
    "unknown": 7,
}


# Lightweight categorizer for items the proposer didn't tag. Keyword-driven;
# good enough for v0. Real classification would use the LLM but cost isn't
# justified for a category guess.
_CATEGORY_HINTS = {
    "fresh_fish": ("salmon", "tuna", "cod", "shrimp", "fish", "tilapia", "scallop", "halibut"),
    "fresh_meat": ("chicken", "beef", "pork", "lamb", "turkey", "bacon", "sausage", "steak", "ground"),
    "fresh_produce": (
        "broccoli", "carrot", "spinach", "lettuce", "kale", "tomato", "onion",
        "garlic", "pepper", "cucumber", "zucchini", "celery", "berry", "berries",
        "apple", "banana", "orange", "lemon", "lime", "avocado", "mushroom",
        "potato", "sweet potato", "yam",
    ),
    "dairy": ("milk", "yogurt", "cheese", "butter", "cream", "sour cream", "kefir", "feta"),
    "bread": ("bread", "tortilla", "pita", "bagel", "bun", "roll", "naan"),
    "frozen": ("frozen ", "ice cream", "popsicle"),
    "pantry": (
        "rice", "pasta", "oat", "flour", "sugar", "salt", "pepper", "spice",
        "oil", "vinegar", "soy sauce", "honey", "syrup", "bean", "lentil",
        "chickpea", "canned", "cracker", "cereal", "nut", "seed", "chia",
        "almond", "peanut butter", "jam",
    ),
}


def categorize_item(name: str) -> str:
    """Best-effort category from item name. Used when the proposer's
    shopping list didn't categorize."""
    n = (name or "").strip().lower()
    if not n:
        return "unknown"
    for cat, hints in _CATEGORY_HINTS.items():
        if any(h in n for h in hints):
            return cat
    return "unknown"


# ── load/save ─────────────────────────────────────────────────────────────


def load_inventory() -> Dict[str, Any]:
    path = get_repo_root() / _INVENTORY_REL
    if not path.is_file():
        return _empty_inventory()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("[grocery] inventory parse failed: %s", e)
        return _empty_inventory()


def save_inventory(inv: Dict[str, Any]) -> None:
    inv["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
    path = get_repo_root() / _INVENTORY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inv, indent=2, ensure_ascii=False), encoding="utf-8")


def _empty_inventory() -> Dict[str, Any]:
    return {"schema_version": 1, "last_updated_utc": None, "items": [], "history": []}


# ── core mutations ────────────────────────────────────────────────────────


def apply_shopping(intention_shopping_pod_id: str) -> Dict[str, Any]:
    """User confirmed a shopping trip. Look up the intention.shopping pod,
    add each item to inventory with appropriate category + decay. Marks
    the pod as applied so it won't get re-applied.

    Returns summary of what was added (or why nothing was).
    """
    from app.assistant.pod_store.pod_store import PodStore
    store = PodStore()
    pod = store.get(intention_shopping_pod_id)
    if pod is None:
        return {"status": "pod_not_found", "added": 0}
    meta = dict(pod.metadata or {})
    if meta.get("inventory_applied_at_utc"):
        return {
            "status": "already_applied",
            "applied_at": meta["inventory_applied_at_utc"],
            "added": 0,
        }

    items = meta.get("items") or []
    if not items:
        return {"status": "no_items_in_pod", "added": 0}

    inv = load_inventory()
    now_utc = datetime.now(timezone.utc)
    added: List[Dict[str, Any]] = []
    for raw_item in items:
        name = _normalize_item_name(raw_item)
        if not name:
            continue
        category = categorize_item(name)
        decay_days = CATEGORY_DECAY_DAYS.get(category, 7)
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "category": category,
            "acquired_at_utc": now_utc.isoformat(),
            "decay_at_utc": (now_utc + timedelta(days=decay_days)).isoformat(),
            "source_intention_id": intention_shopping_pod_id,
            "consumed_at_utc": None,
        }
        inv["items"].append(entry)
        added.append(entry)

    save_inventory(inv)

    # Mark the pod as applied
    meta["inventory_applied_at_utc"] = now_utc.isoformat()
    meta["inventory_added_count"] = len(added)
    pod.metadata = meta
    store.put(pod)

    return {"status": "applied", "added": len(added), "items": [a["name"] for a in added]}


def apply_meal_consumption(intention_meal_pod_id: str) -> Dict[str, Any]:
    """User confirmed a proposed meal happened. Decrement matching ingredients
    from inventory by name match. Marks the pod as applied.
    """
    from app.assistant.pod_store.pod_store import PodStore
    store = PodStore()
    pod = store.get(intention_meal_pod_id)
    if pod is None:
        return {"status": "pod_not_found", "consumed": 0}
    meta = dict(pod.metadata or {})
    if meta.get("inventory_applied_at_utc"):
        return {
            "status": "already_applied",
            "applied_at": meta["inventory_applied_at_utc"],
            "consumed": 0,
        }

    ingredients = meta.get("primary_ingredients") or []
    if not ingredients:
        return {"status": "no_ingredients_in_pod", "consumed": 0}

    inv = load_inventory()
    now_utc = datetime.now(timezone.utc)
    consumed: List[str] = []
    missing: List[str] = []
    for raw_ing in ingredients:
        ing_norm = _normalize_item_name(raw_ing)
        if not ing_norm:
            continue
        found = _find_item_matching(inv["items"], ing_norm)
        if found is None:
            missing.append(ing_norm)
            continue
        found["consumed_at_utc"] = now_utc.isoformat()
        consumed.append(found["name"])

    # Move consumed items to history
    still_active = [i for i in inv["items"] if i.get("consumed_at_utc") is None]
    moved = [i for i in inv["items"] if i.get("consumed_at_utc") is not None]
    inv["items"] = still_active
    inv["history"].extend(moved)

    save_inventory(inv)

    meta["inventory_applied_at_utc"] = now_utc.isoformat()
    meta["inventory_consumed_items"] = consumed
    meta["inventory_missing_items"] = missing
    pod.metadata = meta
    store.put(pod)

    return {
        "status": "applied",
        "consumed": len(consumed),
        "consumed_items": consumed,
        "missing_items": missing,  # ingredients not in inventory
    }


def manual_acquire(items: Iterable[str], *, source_note: str = "manual") -> Dict[str, Any]:
    """User said "we got X" outside a shopping pod context. Add as one-offs."""
    inv = load_inventory()
    now_utc = datetime.now(timezone.utc)
    added: List[str] = []
    for raw in items:
        name = _normalize_item_name(raw)
        if not name:
            continue
        category = categorize_item(name)
        decay_days = CATEGORY_DECAY_DAYS.get(category, 7)
        inv["items"].append({
            "id": uuid.uuid4().hex[:12],
            "name": name,
            "category": category,
            "acquired_at_utc": now_utc.isoformat(),
            "decay_at_utc": (now_utc + timedelta(days=decay_days)).isoformat(),
            "source_intention_id": None,
            "source_note": source_note,
            "consumed_at_utc": None,
        })
        added.append(name)
    save_inventory(inv)
    return {"status": "applied", "added": len(added), "items": added}


def manual_consume(items: Iterable[str], *, reason: str = "manual") -> Dict[str, Any]:
    """User said "we ran out of X" or "I used the Y". Mark matching items
    consumed regardless of any meal pod."""
    inv = load_inventory()
    now_utc = datetime.now(timezone.utc)
    consumed: List[str] = []
    missing: List[str] = []
    for raw in items:
        name = _normalize_item_name(raw)
        if not name:
            continue
        found = _find_item_matching(inv["items"], name)
        if found is None:
            missing.append(name)
            continue
        found["consumed_at_utc"] = now_utc.isoformat()
        found["consumed_reason"] = reason
        consumed.append(found["name"])

    still_active = [i for i in inv["items"] if i.get("consumed_at_utc") is None]
    moved = [i for i in inv["items"] if i.get("consumed_at_utc") is not None]
    inv["items"] = still_active
    inv["history"].extend(moved)
    save_inventory(inv)
    return {"status": "applied", "consumed": len(consumed), "consumed_items": consumed, "missing": missing}


def remove_item(item_id: str, *, reason: str = "manual_ui") -> Dict[str, Any]:
    """Remove one active item by id (the user says it isn't in the fridge).

    Marks it consumed with a reason and moves it to history — the canonical
    mutation behind the inventory-editor UI's per-row delete, so routes never
    hand-edit the JSON. Idempotent: a missing/already-removed id is a no-op.
    """
    inv = load_inventory()
    now_utc = datetime.now(timezone.utc)
    target = next(
        (i for i in inv["items"] if i.get("id") == item_id and i.get("consumed_at_utc") is None),
        None,
    )
    if target is None:
        return {"status": "not_found", "removed": 0}
    target["consumed_at_utc"] = now_utc.isoformat()
    target["consumed_reason"] = reason
    inv["items"] = [i for i in inv["items"] if i.get("consumed_at_utc") is None]
    inv["history"].append(target)
    save_inventory(inv)
    return {"status": "removed", "removed": 1, "name": target.get("name")}


def apply_decay() -> Dict[str, Any]:
    """Move items past decay_at to history. Idempotent."""
    inv = load_inventory()
    now_utc = datetime.now(timezone.utc)
    decayed: List[str] = []
    still_active: List[Dict[str, Any]] = []
    for item in inv["items"]:
        try:
            decay_at = datetime.fromisoformat(item.get("decay_at_utc", "").replace("Z", "+00:00"))
        except Exception:
            still_active.append(item)
            continue
        if decay_at < now_utc:
            item["decayed_at_utc"] = now_utc.isoformat()
            inv["history"].append(item)
            decayed.append(item.get("name", "?"))
        else:
            still_active.append(item)
    inv["items"] = still_active
    save_inventory(inv)
    return {"status": "applied", "decayed": len(decayed), "decayed_items": decayed}


# ── helpers ───────────────────────────────────────────────────────────────


_QTY_PREFIX_RE = re.compile(r"^\s*\d+\s*(lb|lbs|oz|kg|g|cup|cups|tbsp|tsp|piece|pieces|bunch|bunches|head|heads|can|cans|jar|jars|bag|bags|box|boxes|loaf|loaves|carton|cartons|pack|packs)?\s*", re.IGNORECASE)


def _normalize_item_name(raw: str) -> str:
    """Lowercase + strip quantities/qualifiers. '2lb wild salmon' → 'wild salmon'.

    The proposer's shopping items are already mostly clean (per its hard rule
    about lowercase, no qualifiers), but real-world user input is messier.
    Pattern-matching strips obvious quantity prefixes and lowercases.
    Match logic in _find_item_matching is then substring-based.
    """
    if not raw:
        return ""
    s = str(raw).strip()
    s = _QTY_PREFIX_RE.sub("", s)
    return s.lower().strip(" ,.;-")


def _find_item_matching(items: List[Dict[str, Any]], needle: str) -> Optional[Dict[str, Any]]:
    """Substring match against active items by name. Returns the first match
    (oldest acquired_at), so we consume oldest first naturally."""
    needle = needle.lower().strip()
    if not needle:
        return None
    candidates = [i for i in items if i.get("consumed_at_utc") is None]
    matches = []
    for item in candidates:
        name = (item.get("name") or "").lower()
        if needle in name or name in needle:
            matches.append(item)
    if not matches:
        return None
    # Oldest first (FIFO)
    matches.sort(key=lambda i: i.get("acquired_at_utc") or "")
    return matches[0]


# ── readable summary for context_builder ─────────────────────────────────


def render_inventory_summary() -> str:
    """Human-readable inventory snapshot for the meal_proposer's
    inventory_snapshot context slot."""
    inv = load_inventory()
    active = inv.get("items") or []
    if not active:
        return "(inventory currently empty — propose shopping for any meal needing ingredients)"

    now_utc = datetime.now(timezone.utc)
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    soon_decay: List[Dict[str, Any]] = []

    for item in active:
        cat = item.get("category") or "unknown"
        by_category.setdefault(cat, []).append(item)
        try:
            decay_at = datetime.fromisoformat(item.get("decay_at_utc", "").replace("Z", "+00:00"))
            days_left = (decay_at - now_utc).days
            if days_left <= 2:
                soon_decay.append({**item, "days_left": days_left})
        except Exception:
            pass

    lines = [f"Current inventory ({len(active)} items):"]
    for cat in sorted(by_category.keys()):
        items = by_category[cat]
        names = [i.get("name", "?") for i in items]
        lines.append(f"- {cat}: {', '.join(names)}")

    if soon_decay:
        lines.append("")
        lines.append("USE SOON (≤2 days until decay):")
        for item in sorted(soon_decay, key=lambda i: i.get("decay_at_utc", "")):
            lines.append(f"- {item.get('name')} ({item.get('category')}) — {item['days_left']} day(s) left")

    return "\n".join(lines)
