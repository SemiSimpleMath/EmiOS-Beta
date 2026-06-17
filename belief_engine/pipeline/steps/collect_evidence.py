"""
Step 1: Collect evidence from available sources for a given domain.

Sources:
  1. daily_insights   — actionable items from resource_daily_insights.json (last N days)
  2. ticket_signals   — CROSS-DAY behavioral aggregates (dismiss/snooze/accept counts) from
                        timeline_merged.json. Per-event ticket signal is owned by daily_insights
                        (extracted from the same timeline); only the across-days pattern is added.

Writes a structured evidence bundle to the run context.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.time_utils import get_local_timezone
from belief_engine.config import get_domain_config

logger = logging.getLogger(__name__)

# How many days back to look in daily insights / timelines.
_LOOKBACK_DAYS = 14


def _require_domain_config(domain: str):
    """Fetch domain config or fail loudly. A missing domain typically means
    the caller passed a typo'd name — silently returning empty lists would
    let the whole pipeline run with zero evidence and zero error, which
    breaks debuggability."""
    cfg = get_domain_config(domain)
    if cfg is None:
        raise ValueError(
            f"Unknown belief-engine domain {domain!r} — no entry in domain config. "
            f"Check belief_engine.config.get_domain_config."
        )
    return cfg


def _domain_tags(domain: str) -> List[str]:
    return list(_require_domain_config(domain).tags)


def _domain_ticket_types(domain: str) -> List[str]:
    return list(_require_domain_config(domain).ticket_types)


@dataclass
class EvidenceItem:
    source_type: str          # daily_insights | ticket_rejection | ticket_acceptance | kg_edge
    source_date: str          # YYYY-MM-DD
    source_ref: Optional[str]
    signal_type: str          # confirms | qualifies | contradicts | rejects
    summary: str
    raw_text: Optional[str]
    weight: float             # 0.0–5.0


@dataclass
class EvidenceBundle:
    domain: str
    date_range_start: str
    date_range_end: str
    items: List[EvidenceItem] = field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.items) == 0

    def as_block(self) -> str:
        """Format for LLM consumption."""
        if self.is_empty():
            return "(no evidence found)"
        lines = [f"Evidence for domain '{self.domain}' ({self.date_range_start} → {self.date_range_end}):\n"]
        for i, item in enumerate(self.items, 1):
            lines.append(
                f"{i}. [{item.source_type} | {item.source_date} | {item.signal_type} | weight={item.weight:.1f}]"
            )
            lines.append(f"   {item.summary}")
        return "\n".join(lines)


def _day_context_root() -> Path:
    from app.assistant.routine_manager.utils import resources_dir
    return resources_dir().parent / "day_context"


def _date_range(lookback_days: int):
    """Yield YYYY-MM-DD strings from today back N days."""
    tz = get_local_timezone()
    today = datetime.now(tz).date()
    for i in range(lookback_days):
        yield str(today - timedelta(days=i))


def _collect_daily_insights(domain: str, lookback_days: int) -> List[EvidenceItem]:
    tags = _domain_tags(domain)
    items: List[EvidenceItem] = []
    root = _day_context_root()

    for date_str in _date_range(lookback_days):
        year, month = date_str[:4], date_str[5:7]
        path = root / year / month / date_str / "resource_daily_insights.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("[CollectEvidence] skip %s: %s", path, exc)
            continue

        for action in data.get("actionable_information") or []:
            action_tags = action.get("tags") or []
            if not any(t in tags for t in action_tags):
                continue
            scope = action.get("temporal_scope", "chronic")
            evidence_texts = action.get("evidence") or []
            raw = evidence_texts[0] if evidence_texts else None
            fact_summary = action.get("fact_summary", "")
            # When a verbatim user quote is available, use it as the primary summary
            # so the belief engine sees the user's actual words, not a paraphrase.
            # The generated fact_summary is preserved as a brief label prefix.
            if raw:
                summary = f"[{fact_summary}] User said: {raw}"
            else:
                summary = fact_summary
            items.append(EvidenceItem(
                source_type="daily_insights",
                source_date=date_str,
                source_ref=None,
                signal_type="confirms" if scope == "chronic" else "qualifies",
                summary=summary,
                raw_text=raw,
                weight=3.0 if scope == "chronic" else 1.5,
            ))
    return items


def _collect_ticket_signals(domain: str, lookback_days: int) -> List[EvidenceItem]:
    """
    Collect CROSS-DAY behavioral patterns from ticket events.

    Ticket events also flow through daily_insights (the daily_timeline_insights agent
    extracts them from the same timeline_merged.json), so the per-event / commented signal
    is already covered there — re-emitting it here would double-count it. What insights
    structurally CANNOT see is a pattern across days, because each run sees a single day. So
    this path keeps ONLY the cross-day aggregate: how many times, over the window, the user
    dismissed / snoozed / accepted a given suggestion type. That is what reveals a wrong
    cadence even when the user never comments.
    """
    ticket_types = _domain_ticket_types(domain)
    if not ticket_types:
        return []

    from collections import defaultdict

    # Cross-day tally: key=(stype, outcome) → count + the set of days it occurred on.
    tally: Dict[tuple, Dict] = defaultdict(lambda: {"count": 0, "dates": set()})

    root = _day_context_root()

    for date_str in _date_range(lookback_days):
        year, month = date_str[:4], date_str[5:7]
        path = root / year / month / date_str / "timeline_merged.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("[CollectEvidence] skip timeline %s: %s", path, exc)
            continue

        for entry in data.get("timeline") or []:
            if entry.get("type") != "ticket":
                continue
            stype = entry.get("suggestion_type", "")
            if not any(stype == t or stype.startswith(t) for t in ticket_types):
                continue

            state = entry.get("state", "")
            user_action = entry.get("user_action", "")
            if state == "dismissed" or user_action == "skip":
                outcome = "rejected"
            elif state == "expired":
                outcome = "snoozed"
            elif state == "accepted":
                outcome = "accepted"
            else:
                continue
            tally[(stype, outcome)]["count"] += 1
            tally[(stype, outcome)]["dates"].add(date_str)

    # Emit a signal only when the count over the window indicates a real pattern. Bare
    # acceptances of routine nudges add nothing beyond what daily_insights already captures.
    _SKIP_BARE_ACCEPTANCE_TYPES = {
        "finger_stretch", "standing_break", "hydration", "movement", "stretch", "walk",
    }
    items: List[EvidenceItem] = []
    for (stype, outcome), stats in tally.items():
        count = stats["count"]
        days = len(stats["dates"])
        date_range_str = f"{min(stats['dates'])} – {max(stats['dates'])}"
        if outcome == "rejected" and count >= 2:
            items.append(EvidenceItem(
                source_type="ticket_rejection",
                source_date=max(stats["dates"]),
                source_ref=None,
                signal_type="rejects",
                summary=f"User dismissed '{stype}' {count} time(s) across {days} day(s) ({date_range_str}) — a recurring rejection; the suggestion or its cadence is likely unwanted.",
                raw_text=None,
                weight=3.0 + min((count - 2) * 0.5, 2.0),  # 3.0 at 2x, up to 5.0
            ))
        elif outcome == "snoozed" and count >= 3:
            items.append(EvidenceItem(
                source_type="ticket_rejection",
                source_date=max(stats["dates"]),
                source_ref=None,
                signal_type="qualifies",
                summary=f"User snoozed/deferred '{stype}' without completing {count} time(s) across {days} day(s) ({date_range_str}) — current pacing may be too frequent.",
                raw_text=None,
                weight=2.5 + min((count - 3) * 0.5, 2.0),  # 2.5 at 3x, up to 4.5 at 7x+
            ))
        elif outcome == "accepted" and stype not in _SKIP_BARE_ACCEPTANCE_TYPES:
            items.append(EvidenceItem(
                source_type="ticket_acceptance",
                source_date=max(stats["dates"]),
                source_ref=None,
                signal_type="confirms",
                summary=f"User accepted '{stype}' {count} time(s) over {days} day(s) ({date_range_str}).",
                raw_text=None,
                weight=1.0 + min(count / 10.0, 2.0),
            ))

    return items


class CollectEvidenceStep:
    name = "collect_evidence"

    def __init__(self, domain: str, lookback_days: int = _LOOKBACK_DAYS) -> None:
        self.domain = domain
        self.lookback_days = lookback_days

    def inputs(self, ctx: Any) -> list:
        return [f"day_context/<date>/resource_daily_insights.json (last {self.lookback_days} days)",
                f"day_context/<date>/timeline_merged.json (last {self.lookback_days} days)"]

    def outputs(self, ctx: Any) -> list:
        return []

    def run(self, ctx: Any) -> EvidenceBundle:
        tz = get_local_timezone()
        today = datetime.now(tz).date()
        start = str(today - timedelta(days=self.lookback_days - 1))

        bundle = EvidenceBundle(
            domain=self.domain,
            date_range_start=start,
            date_range_end=str(today),
        )

        insights = _collect_daily_insights(self.domain, self.lookback_days)
        tickets = _collect_ticket_signals(self.domain, self.lookback_days)
        bundle.items.extend(insights)
        bundle.items.extend(tickets)

        # Sort by date descending (most recent first).
        bundle.items.sort(key=lambda x: x.source_date, reverse=True)

        logger.info(
            "[CollectEvidenceStep] domain=%s items=%d (insights=%d tickets=%d)",
            self.domain, len(bundle.items), len(insights), len(tickets),
        )
        ctx.evidence_bundle = bundle
        return bundle
