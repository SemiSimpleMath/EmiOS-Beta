"""The week's grocery order: one row per week in emi.db (owner, 2026-11-14).

The weekly meal planner writes the list it planned (`save_planned`): every item to order from Ralphs, meal ingredients
and the standing staples needed this week, each with its reason, and the staples it chose not to buy, each with why.
The owner reviews it on /meals, edits it and approves it with an order time; at that time the assistant builds the
DoorDash cart, he confirms it, the order is placed and the receipt fills the inventory. This module holds the list
and its state; the page, the order work and the receipt step read and write it here.

Status: draft (planned, editable) -> approved (owner set the order time) -> ordered -> delivered; or skipped (owner
skipped the week). A planner run replaces a draft; it never overwrites a list the owner has approved.

`edits` records each owner change as {"at": UTC ISO, "change": "added" | "removed" | "changed", "text": what, in words}
so next week's planner sees what he corrected. Each item carries an `id`, so an edit names the item, not its row.

The default order time (weekday + local time) is the owner's setting in resource_grocery_settings.json.

The order work records its two outcomes here (the grocery_order tool): `mark_ordered` once the order is placed (with
the store's order number and total, in words), and `mark_delivered` with what the receipt says arrived, substitutions
included; those items enter the kitchen inventory (grocery_inventory.manual_acquire), so next week's planner sees them
as bought.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.assistant.subconscious.db import connect as _connect

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS grocery_lists (
        week_start TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        items TEXT NOT NULL,
        skipped_staples TEXT NOT NULL,
        planner_note TEXT NOT NULL DEFAULT '',
        plan_pod_id TEXT,
        edits TEXT NOT NULL DEFAULT '[]',
        order_at TEXT,
        approved_at TEXT,
        ordered_at TEXT,
        order_note TEXT,
        delivered_at TEXT,
        delivered_items TEXT,
        delivery_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
]

STATUSES = ("draft", "approved", "ordered", "delivered", "skipped")
_ITEM_FIELDS = ("name", "quantity", "category", "reason")
_SETTINGS_REL = "subconscious/resource_grocery_settings.json"
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def ensure_schema(connect=None) -> None:
    with (connect or _connect)(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> Dict[str, Any]:
    d = dict(r)
    for k in ("items", "skipped_staples", "edits"):
        d[k] = json.loads(d[k])
    d["delivered_items"] = json.loads(d["delivered_items"]) if d["delivered_items"] else None
    return d


def _clean_items(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out = []
    for it in items:
        name = str(it.get("name") or "").strip()
        if not name:
            raise ValueError(f"grocery list: an item has no name: {it!r}")
        out.append({k: str(it.get(k) or "").strip() for k in _ITEM_FIELDS} | {"name": name, "id": _new_id()})
    return out


def _new_id() -> str:
    return uuid.uuid4().hex[:10]


def get(week_start: str, *, connect=None) -> Optional[Dict[str, Any]]:
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        r = c.execute("SELECT * FROM grocery_lists WHERE week_start = ?", (week_start,)).fetchone()
    return _row(r) if r else None


def save_planned(week_start: str, *, items: List[Dict[str, Any]], skipped_staples: List[Dict[str, Any]],
                 planner_note: str = "", plan_pod_id: Optional[str] = None, connect=None) -> Dict[str, Any]:
    """Store the planner's list for the week as a draft. Replaces an existing draft (a re-run of the planner);
    raises when the owner has already moved the week past draft, so a re-run never undoes his approval."""
    connect = connect or _connect
    ensure_schema(connect)
    clean = _clean_items(items)
    skipped = [{"name": str(s.get("name") or "").strip(), "reason": str(s.get("reason") or "").strip()}
               for s in skipped_staples if str(s.get("name") or "").strip()]
    now = _now()
    with connect(True) as c:
        existing = c.execute("SELECT status FROM grocery_lists WHERE week_start = ?", (week_start,)).fetchone()
        if existing is not None and existing["status"] != "draft":
            raise ValueError(f"grocery list for {week_start} is {existing['status']}; a planner run does not replace it")
        c.execute("DELETE FROM grocery_lists WHERE week_start = ?", (week_start,))
        c.execute(
            "INSERT INTO grocery_lists (week_start, status, items, skipped_staples, planner_note, plan_pod_id, edits, "
            "created_at, updated_at) VALUES (?, 'draft', ?, ?, ?, ?, '[]', ?, ?)",
            (week_start, json.dumps(clean), json.dumps(skipped), planner_note.strip(), plan_pod_id, now, now))
    _report(week_start, "ready", connect=connect)
    return {"week_start": week_start, "status": "draft", "item_count": len(clean), "skipped_count": len(skipped)}


# ── what the brain hears ────────────────────────────────────────────────────

def _report(week_start: str, change: str, *, connect) -> None:
    """Tell the brain about a step of the week's list (brain_inbox source 'grocery') and wake it. Written after the
    step's own transaction: the list's state is committed first, the brain reads it after."""
    from app.assistant.subconscious import brain_inbox, brain_wake
    connect = connect or _connect          # the list and its event share one database
    row = get(week_start, connect=connect)
    brain_inbox.add_grocery_event(week_start=week_start, change=change, text=event_text(row, change),
                                  at=datetime.fromisoformat(row["updated_at"]),
                                  speaker="the weekly meal planner" if change == "ready" else "the owner",
                                  connect=connect)
    brain_wake.poke()


def event_text(row: Dict[str, Any], change: str) -> str:
    """The step in words, with the whole list where it matters, as the gate and the brain read it."""
    from app.assistant.utils.time_utils import local_time_text
    week = datetime.fromisoformat(row["week_start"]).strftime("%a %Y-%m-%d")
    page = f"/meals?week={row['week_start']}#grocery"
    items = "\n".join(f"- {_item_text(i)}: {i['reason']}" for i in row["items"]) or "(no items)"
    if change == "ready":
        skipped = "\n".join(f"- {s['name']}: {s['reason']}" for s in row["skipped_staples"]) or "(none)"
        note = f"\nThe planner's note: {row['planner_note']}" if row["planner_note"] else ""
        return (f"The grocery list for the week starting {week} is ready for the owner to review on the meals page "
                f"({page}). The owner edits it there and approves it with the time it is ordered from Ralphs through "
                f"DoorDash; nothing is ordered before the owner approves it.{note}\n"
                f"Items ({len(row['items'])}):\n{items}\nStaples left off:\n{skipped}")
    if change == "approved":
        return (f"The owner approved the grocery list for the week starting {week} on the meals page ({page}). It is "
                f"to be ordered from Ralphs through DoorDash at {local_time_text(row['order_at'])}, and the owner "
                f"confirms the built cart and its total before checkout.\nItems ({len(row['items'])}):\n{items}")
    if change == "skipped":
        return f"The owner skipped the grocery order for the week starting {week} on the meals page: nothing is ordered."
    if change == "reopened":
        return (f"The owner reopened the grocery list for the week starting {week} on the meals page to change it. It "
                f"is no longer approved; nothing is ordered until the owner approves it again.")
    raise ValueError(f"unknown grocery list change {change!r}")


def _change(week_start: str, mutate, *, allowed: tuple, connect) -> Dict[str, Any]:
    """Read the week's list, apply `mutate(row, now)` (it edits the row in place and returns the edit records to
    append), and write it back in one transaction. Refused unless the list's status is in `allowed`."""
    connect = connect or _connect
    ensure_schema(connect)
    now = _now()
    with connect(True) as c:
        r = c.execute("SELECT * FROM grocery_lists WHERE week_start = ?", (week_start,)).fetchone()
        if r is None:
            raise ValueError(f"no grocery list for the week of {week_start}")
        row = _row(r)
        if row["status"] not in allowed:
            raise ValueError(f"the grocery list for {week_start} is {row['status']}; this change needs {allowed}")
        row["edits"].extend({"at": now} | e for e in mutate(row, now))
        c.execute("UPDATE grocery_lists SET status = ?, items = ?, edits = ?, order_at = ?, approved_at = ?, "
                  "ordered_at = ?, order_note = ?, delivered_at = ?, delivered_items = ?, delivery_note = ?, "
                  "updated_at = ? WHERE week_start = ?",
                  (row["status"], json.dumps(row["items"]), json.dumps(row["edits"]), row["order_at"],
                   row["approved_at"], row["ordered_at"], row["order_note"], row["delivered_at"],
                   json.dumps(row["delivered_items"]) if row["delivered_items"] is not None else None,
                   row["delivery_note"], now, week_start))
    return row


def _item(row: Dict[str, Any], item_id: str) -> Dict[str, str]:
    for it in row["items"]:
        if it["id"] == item_id:
            return it
    raise ValueError(f"no item {item_id} on the grocery list for {row['week_start']}")


def add_item(week_start: str, *, name: str, quantity: str = "", category: str = "", reason: str = "",
             connect=None) -> Dict[str, Any]:
    """The owner adds an item to a draft list."""
    item = _clean_items([{"name": name, "quantity": quantity, "category": category,
                          "reason": reason or "added by the owner"}])[0]

    def mutate(row, now):
        row["items"].append(item)
        return [{"change": "added", "text": _item_text(item)}]
    return _change(week_start, mutate, allowed=("draft",), connect=connect)


def remove_item(week_start: str, item_id: str, *, connect=None) -> Dict[str, Any]:
    """The owner removes an item from a draft list."""
    def mutate(row, now):
        item = _item(row, item_id)
        row["items"].remove(item)
        return [{"change": "removed", "text": _item_text(item)}]
    return _change(week_start, mutate, allowed=("draft",), connect=connect)


def set_quantity(week_start: str, item_id: str, quantity: str, *, connect=None) -> Dict[str, Any]:
    """The owner changes an item's quantity on a draft list. The same quantity records nothing."""
    quantity = quantity.strip()

    def mutate(row, now):
        item = _item(row, item_id)
        if item["quantity"] == quantity:
            return []
        was = item["quantity"] or "no quantity"
        item["quantity"] = quantity
        return [{"change": "changed", "text": f"{item['name']}: {was} -> {quantity or 'no quantity'}"}]
    return _change(week_start, mutate, allowed=("draft",), connect=connect)


def approve(week_start: str, order_at_utc: datetime, *, connect=None) -> Dict[str, Any]:
    """The owner approves the draft list and sets when it is ordered (an aware datetime)."""
    if order_at_utc.tzinfo is None:
        raise ValueError("approve: order_at_utc must be timezone-aware")

    def mutate(row, now):
        row.update(status="approved", order_at=order_at_utc.astimezone(timezone.utc).isoformat(), approved_at=now)
        return []
    row = _change(week_start, mutate, allowed=("draft",), connect=connect)
    _report(week_start, "approved", connect=connect)
    return row


def skip(week_start: str, *, connect=None) -> Dict[str, Any]:
    """The owner skips the week: nothing is ordered."""
    def mutate(row, now):
        row.update(status="skipped", order_at=None, approved_at=None)
        return []
    row = _change(week_start, mutate, allowed=("draft", "approved"), connect=connect)
    _report(week_start, "skipped", connect=connect)
    return row


def reopen(week_start: str, *, connect=None) -> Dict[str, Any]:
    """Back to an editable draft (an approved or skipped week that has not been ordered)."""
    def mutate(row, now):
        row.update(status="draft", order_at=None, approved_at=None)
        return []
    row = _change(week_start, mutate, allowed=("approved", "skipped"), connect=connect)
    _report(week_start, "reopened", connect=connect)
    return row


def mark_ordered(week_start: str, *, note: str, connect=None) -> Dict[str, Any]:
    """The order work placed the approved order. `note` is what the store confirmed, in words: its order number and
    total, the expected delivery."""
    if not note.strip():
        raise ValueError("mark_ordered: say what the store confirmed (order number, total)")

    def mutate(row, now):
        row.update(status="ordered", ordered_at=now, order_note=note.strip())
        return []
    return _change(week_start, mutate, allowed=("approved",), connect=connect)


def mark_delivered(week_start: str, *, items: List[str], note: str = "", connect=None) -> Dict[str, Any]:
    """The receipt came: `items` is what it says arrived, substitutions included, each as the receipt names it. The
    list becomes delivered, then the items enter the kitchen inventory."""
    from app.assistant.subconscious import grocery_inventory
    received = [str(i).strip() for i in items if str(i).strip()]
    if not received:
        raise ValueError("mark_delivered: list what the receipt says arrived")

    def mutate(row, now):
        row.update(status="delivered", delivered_at=now, delivered_items=received, delivery_note=note.strip())
        return []
    row = _change(week_start, mutate, allowed=("ordered",), connect=connect)
    grocery_inventory.manual_acquire(received, source_note=f"grocery delivery, week of {week_start}")
    return row


def latest(*, statuses: tuple, connect=None) -> Optional[Dict[str, Any]]:
    """The newest week's list whose status is one of `statuses`, or None."""
    connect = connect or _connect
    ensure_schema(connect)
    marks = ",".join("?" * len(statuses))
    with connect(False) as c:
        r = c.execute(f"SELECT * FROM grocery_lists WHERE status IN ({marks}) ORDER BY week_start DESC LIMIT 1",
                      statuses).fetchone()
    return _row(r) if r else None


def default_order_at(week_start: str) -> datetime:
    """The owner's default order time for the week starting `week_start` (ISO Monday), as an aware UTC datetime:
    the configured weekday and local time within that week."""
    from app.assistant.utils import resource_files
    from app.assistant.utils.time_utils import local_to_utc

    setting = resource_files.read(_SETTINGS_REL)["default_order"]
    weekday = _WEEKDAYS.index(setting["weekday"])
    hour, minute = (int(p) for p in setting["time"].split(":"))
    day = datetime.fromisoformat(week_start) + timedelta(days=weekday)
    return local_to_utc(day.replace(hour=hour, minute=minute))


def recent(*, before_week: str, weeks: int = 4, connect=None) -> List[Dict[str, Any]]:
    """The lists of the `weeks` weeks before `before_week` (ISO Monday), newest first."""
    connect = connect or _connect
    ensure_schema(connect)
    since = (datetime.fromisoformat(before_week) - timedelta(weeks=weeks)).date().isoformat()
    with connect(False) as c:
        rows = c.execute("SELECT * FROM grocery_lists WHERE week_start >= ? AND week_start < ? "
                         "ORDER BY week_start DESC", (since, before_week)).fetchall()
    return [_row(r) for r in rows]


def render_recent_for_planner(*, before_week: str, connect=None) -> str:
    """Past weeks' lists for the weekly planner: what was on each list, what the owner changed, and its status."""
    lists = recent(before_week=before_week, connect=connect)
    if not lists:
        return "(no earlier grocery lists yet)"
    lines: List[str] = []
    for gl in lists:
        lines.append(f"Week of {gl['week_start']} ({gl['status']}):")
        lines.append("  items: " + (", ".join(_item_text(i) for i in gl["items"]) or "(none)"))
        for e in gl["edits"]:
            lines.append(f"  owner {e['change']}: {e['text']}")
    return "\n".join(lines)


def _item_text(item: Dict[str, str]) -> str:
    return f"{item['name']} ({item['quantity']})" if item.get("quantity") else item["name"]
