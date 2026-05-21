"""Render the current weekly meal plan + shopping intent into an email.

Pure projection layer: takes the latest plan.weekly_meals pod and a
pre-formatted shopping_text block, returns (subject, body). Plain text
only — Gmail's plain-text rendering is fine; no HTML needed.

The /meals route uses this; future surfaces (digest, SMS, etc.) can too.
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from app.assistant.pod_store.contracts import Pod

_DAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_WINDOW_ORDER = ["breakfast", "lunch", "dinner", "snack"]


def render_weekly_meal_email(
    plan_pod: Optional[Pod],
    shopping_text: Optional[str] = None,
    *,
    sender_name: str = "Emi",
) -> Tuple[str, str]:
    """Return (subject, body) for a weekly meal plan email.

    Falls back to a clear "no plan yet" body if plan_pod is None — the
    caller decides whether to send anyway."""
    if plan_pod is None:
        subject = "Weekly meal plan (no plan yet)"
        body = (
            "No weekly meal plan has been generated yet.\n\n"
            "Run the weekly meal planner first.\n\n"
            f"-- {sender_name}\n"
        )
        return subject, body

    meta = plan_pod.metadata or {}
    week_start = meta.get("week_start_date") or "?"
    anchors: List[str] = list(meta.get("anchor_meals") or [])
    slots: List[dict] = list(meta.get("slots") or [])

    theme = _extract_theme_from_body(plan_pod.body or "")

    subject = f"Weekly meal plan — week of {week_start}"

    lines: List[str] = []
    lines.append(f"WEEKLY MEAL PLAN — week of {week_start}")
    lines.append("=" * (24 + len(week_start)))
    lines.append("")
    if theme:
        lines.append("Theme")
        lines.append("-----")
        lines.append(_wrap_paragraph(theme, indent=2))
        lines.append("")
    if anchors:
        lines.append("Anchor meals this week")
        lines.append("----------------------")
        for a in anchors:
            lines.append(f"  - {a}")
        lines.append("")

    lines.append("Plan by day")
    lines.append("-----------")
    grouped = _group_slots_by_date(slots)
    for date in sorted(grouped.keys()):
        day_slots = grouped[date]
        dow = day_slots[0].get("day_of_week") if day_slots else ""
        header = f"{dow} {date}" if dow else date
        lines.append(header)
        for window in _WINDOW_ORDER:
            for slot in day_slots:
                if (slot.get("meal_window") or "") == window:
                    lines.append("  " + _format_slot_line(slot))
        # Catch any slot whose meal_window isn't in the standard order
        for slot in day_slots:
            mw = slot.get("meal_window") or ""
            if mw not in _WINDOW_ORDER:
                lines.append("  " + _format_slot_line(slot))
        lines.append("")

    shopping_text_stripped = (shopping_text or "").strip()
    if shopping_text_stripped:
        lines.append("Shopping needs")
        lines.append("--------------")
        for sline in shopping_text_stripped.splitlines():
            lines.append("  " + sline if sline.strip() else "")
        lines.append("")

    lines.append(f"-- {sender_name}")
    lines.append("")
    return subject, "\n".join(lines)


def _format_slot_line(slot: dict) -> str:
    window = slot.get("meal_window") or "?"
    slot_type = slot.get("slot_type") or "?"
    dish = (slot.get("dish") or "").strip()
    leftover_from = (slot.get("leftover_from_date") or "").strip()
    notes = (slot.get("notes") or "").strip()

    head = f"{window:>9}: {slot_type}"
    tail_parts: List[str] = []
    if dish:
        tail_parts.append(dish)
    if leftover_from:
        tail_parts.append(f"leftover from {leftover_from}")
    if notes:
        tail_parts.append(notes)
    if tail_parts:
        return head + " — " + " | ".join(tail_parts)
    return head


def _group_slots_by_date(slots: Iterable[dict]) -> dict:
    out: dict = {}
    for s in slots:
        d = s.get("date") or "?"
        out.setdefault(d, []).append(s)
    return out


def _extract_theme_from_body(body: str) -> str:
    """The plan pod body has '**Theme:** <text>' on one line; extract it."""
    marker = "**Theme:**"
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(marker):
            return line[len(marker):].strip()
    return ""


def consolidate_intention_shopping(pods: Iterable[Pod]) -> List[str]:
    """Collect unique items across all intention.shopping pods, in
    first-seen order. Case-insensitive dedup. Used by the service layer
    to build the ad-hoc additions block."""
    seen: dict = {}
    for pod in pods:
        items = (pod.metadata or {}).get("items") or []
        for raw in items:
            item = str(raw).strip()
            if not item:
                continue
            key = item.lower()
            if key not in seen:
                seen[key] = item
    return list(seen.values())


def _wrap_paragraph(text: str, indent: int = 0) -> str:
    """Simple 78-col wrap with a left indent. Email-friendly."""
    import textwrap
    pad = " " * indent
    wrapped = textwrap.fill(text, width=78 - indent)
    return "\n".join(pad + line for line in wrapped.splitlines())
