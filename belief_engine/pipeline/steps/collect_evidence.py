"""
Step 1: Collect evidence from available sources.

Sources:
  1. daily_insights   — actionable items from resource_daily_insights.json (last N days)
  2. ticket_context   — complete dated ticket records for interpretation. Daily insights
                        own the underlying observations; this rolling view adds no weight.

  3. weekly_insights — full candidates, global reconciliation only, zero independent weight.

The default run is GLOBAL (domain=None): one bundle over the union of every enabled domain's
tags and ticket types, each item carrying its own insight tags so the updater can file the
belief under a primary area. A per-domain slice (domain="routine") is still available for
scripts and inspection. Writes a structured evidence bundle to the run context.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger
import json
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.utils.time_utils import get_local_timezone
from belief_engine.config import get_domain_config

logger = get_logger(__name__)

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


def _domain_tags(domain: Optional[str]) -> List[str]:
    """Tags admitted for this run: one domain's, or (domain=None) the union over every
    enabled domain — the global pass sees every insight any domain would have seen."""
    if domain is None:
        from belief_engine.config import list_enabled_domains
        seen: List[str] = []
        for cfg in list_enabled_domains():
            for t in cfg.tags:
                if t not in seen:
                    seen.append(t)
        return seen
    return list(_require_domain_config(domain).tags)


def _domain_ticket_types(domain: Optional[str]) -> List[str]:
    if domain is None:
        from belief_engine.config import list_enabled_domains
        seen: List[str] = []
        for cfg in list_enabled_domains():
            for t in cfg.ticket_types:
                if t not in seen:
                    seen.append(t)
        return seen
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
    # The insight's own tags (a subset of the domain vocab). Rendered to the updater so it
    # can file each belief under a primary area; empty for ticket aggregates.
    tags: List[str] = field(default_factory=list)


@dataclass
class EvidenceBundle:
    domain: str               # a domain id, or "all" for the global pass
    date_range_start: str
    date_range_end: str
    items: List[EvidenceItem] = field(default_factory=list)

    def is_empty(self) -> bool:
        return len(self.items) == 0

    def as_block(self) -> str:
        """Format for LLM consumption."""
        if self.is_empty():
            return "(no evidence found)"
        where = "all areas" if self.domain == "all" else f"domain '{self.domain}'"
        lines = [f"Evidence for {where} ({self.date_range_start} → {self.date_range_end}):\n"]
        for i, item in enumerate(self.items, 1):
            tags = f" | tags={','.join(item.tags)}" if item.tags else ""
            lines.append(
                f"{i}. [{item.source_type} | {item.source_date} | {item.signal_type} | weight={item.weight:.1f}{tags}]"
            )
            lines.append(f"   {item.summary}")
            if item.raw_text:
                lines.append(f"   Source text: {item.raw_text}")
            if item.source_ref:
                lines.append(f"   Source reference: {item.source_ref}")
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


def _collect_daily_insights(domain: Optional[str], lookback_days: int) -> List[EvidenceItem]:
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
            raw = (evidence_texts[0] if len(evidence_texts) == 1 else json.dumps(evidence_texts, ensure_ascii=False)) if evidence_texts else None
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
                source_ref=None,  # preserve legacy identity; source date and full content remain available
                signal_type="confirms" if scope == "chronic" else "qualifies",
                summary=summary,
                raw_text=raw,
                weight=3.0 if scope == "chronic" else 1.5,
                tags=[t for t in action_tags if t in tags],
            ))
    return items


def _collect_ticket_signals(domain: Optional[str], lookback_days: int) -> List[EvidenceItem]:
    """Supply complete dated ticket context for LLM interpretation, with no extra weight.

    Daily insights already cover these events. Rolling windows are context, not new
    observations. Transport state alone does not establish intent or completion.
    """
    ticket_types = _domain_ticket_types(domain)
    if not ticket_types:
        return []
    grouped = {}
    root = _day_context_root()
    for date_str in _date_range(lookback_days):
        path = root / date_str[:4] / date_str[5:7] / date_str / "timeline_merged.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("timeline") or []:
            if entry.get("type") != "ticket":
                continue
            stype = entry.get("suggestion_type", "")
            if not any(stype == t or stype.startswith(t) for t in ticket_types):
                continue
            grouped.setdefault(stype, []).append({'source_date':date_str,'ticket':entry})
    items = []
    for stype, events in sorted(grouped.items()):
        events.sort(key=lambda event: json.dumps(event, sort_keys=True, ensure_ascii=False))
        content = json.dumps(events, sort_keys=True, ensure_ascii=False)
        items.append(EvidenceItem(
            source_type="ticket_context", source_date=max(e['source_date'] for e in events),
            source_ref="ticket_context:" + hashlib.sha256(content.encode()).hexdigest(),
            signal_type="qualifies", summary=f"Dated ticket context for {stype}",
            raw_text=content, weight=0.0))
    return items


def _collect_weekly_insights() -> List[EvidenceItem]:
    """Weekly interpretations enter reconciliation without independent support weight."""
    from app.assistant.utils.path_utils import get_resources_dir
    path = get_resources_dir() / 'weekly_insights_pipeline_outputs' / 'resource_weekly_insights_latest.json'
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if not data.get('week_start') or not data.get('week_end'):
        raise ValueError('Weekly insights lack source-period dates')
    result = []
    for candidate in (data.get('insights') or {}).get('belief_candidates') or []:
        record = {'week_start':data['week_start'],'week_end':data['week_end'],
                  'days_included':data.get('days_included',[]),'candidate':candidate}
        content = json.dumps(record,sort_keys=True,ensure_ascii=False)
        result.append(EvidenceItem(source_type='weekly_insights',source_date=data['week_end'],
            source_ref='weekly_insights:'+hashlib.sha256(content.encode()).hexdigest(),
            signal_type='qualifies',summary=candidate.get('statement',''),raw_text=content,weight=0.0,
            tags=[]))
    return result


class CollectEvidenceStep:
    name = "collect_evidence"

    def __init__(self, domain: Optional[str] = None, lookback_days: int = _LOOKBACK_DAYS) -> None:
        # domain=None is the global pass (every enabled domain's tags/ticket types).
        self.domain = domain
        self.lookback_days = lookback_days

    def inputs(self, ctx: Any) -> list:
        return [f"day_context/<date>/resource_daily_insights.json (last {self.lookback_days} days)",
                f"day_context/<date>/timeline_merged.json (last {self.lookback_days} days)",
                "weekly_insights_pipeline_outputs/resource_weekly_insights_latest.json"]

    def outputs(self, ctx: Any) -> list:
        return []

    def run(self, ctx: Any) -> EvidenceBundle:
        tz = get_local_timezone()
        today = datetime.now(tz).date()
        start = str(today - timedelta(days=self.lookback_days - 1))

        bundle = EvidenceBundle(
            domain=self.domain or "all",
            date_range_start=start,
            date_range_end=str(today),
        )

        insights = _collect_daily_insights(self.domain, self.lookback_days)
        tickets = _collect_ticket_signals(self.domain, self.lookback_days)
        bundle.items.extend(insights)
        bundle.items.extend(tickets)
        weekly = _collect_weekly_insights() if self.domain is None else []
        bundle.items.extend(weekly)

        # Sort by date descending (most recent first).
        bundle.items.sort(key=lambda x: x.source_date, reverse=True)

        logger.info(
            "[CollectEvidenceStep] domain=%s items=%d (insights=%d tickets=%d weekly=%d)",
            self.domain, len(bundle.items), len(insights), len(tickets), len(weekly),
        )
        ctx.evidence_bundle = bundle
        return bundle
