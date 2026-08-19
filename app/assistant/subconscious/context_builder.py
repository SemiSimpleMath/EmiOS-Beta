"""Build the injected context dict for the subconscious noticer.

Pulls real data from existing sources (diet log resource, pod_store, KG entity
cards, calendar, unified_log) and emits a flat dict mapping each
`user_context_items` key (see noticer/config.yaml) to a human-readable string.

Data sources that don't yet have clean access (sleep_log, ambient_state)
return "(no <kind> data available)" rather than failing — the noticer's
prompt is written to handle partial context.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir
from app.assistant.utils.identity_names import get_required_primary_user_name
from app.assistant.utils.time_utils import get_local_time, get_local_time_str

logger = get_logger(__name__)


_NO_DATA = "(no data available)"
_NO_DATA_FMT = "(no {kind} data available)"


def build_noticer_context(
    *,
    trigger_mode: str = "daily",
    household_members: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Assemble the full context dict for one noticer tick.

    Returns a dict keyed by the user_context_items names in the noticer's
    config.yaml. Each value is a string ready to interpolate into user.j2.
    """
    now_local = get_local_time()
    now_utc = datetime.now(timezone.utc)

    household_members = household_members or _resolve_household_members()

    from app.assistant.utils.identity_names import get_assistant_persona_block
    return {
        "assistant_persona": get_assistant_persona_block(),
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "trigger_mode": trigger_mode,
        "diet_log_7d": _build_diet_log(),
        "recent_chat_clusters": _build_recent_chat_clusters(now_utc=now_utc),
        "recent_friction_signals": _build_recent_friction_signals(now_utc=now_utc),
        "recent_passing_mentions": _build_recent_passing_mentions(now_utc=now_utc),
        "calendar_today_tomorrow": _build_calendar_today_tomorrow(now_local=now_local),
        "calendar_week_summary": _build_calendar_week_summary(now_local=now_local),
        "sleep_log_7d": _build_sleep_log(),
        "activity_log_7d": _build_activity_log(),
        "family_roster": _build_family_roster(household_members),
        "kg_household_digests": _build_kg_household_digests(household_members),
        "ambient_state_digest": _build_ambient_state_digest(household_members),
        "concerns_register_active": _build_concerns_register_active(),
        "question_mailbox": _build_question_mailbox(),
        "exploration_outcomes_30d": _build_exploration_outcomes(now_utc=now_utc),
        "dayflow_recent": _build_dayflow_recent(now_utc=now_utc),
        "watchlist_summary": _build_watchlist_summary(),
        "calendar_30_90d": _build_calendar_30_90d(now_local=now_local),
        "recurring_obligations": _build_recurring_obligations(),
        "family_graph_digest": _build_family_graph_digest(),
    }


# ── individual section builders ──────────────────────────────────────────


def _resolve_household_members() -> List[str]:
    """Read user_data → first_name + important_people (close relations) to
    identify the household roster.

    Future: refine with KG cohabits-with / lives-with / is-family-of edges.
    """
    user_data_path = get_resources_dir() / "user" / "resource_user_data.json"
    data = json.loads(user_data_path.read_text(encoding="utf-8"))
    members = [get_required_primary_user_name()]
    # important_people: [{name, relationship, birthdate}, ...]
    household_relationships = {
        "wife", "husband", "spouse", "partner",
        "son", "daughter", "child", "kid",
    }
    for p in (data.get("important_people") or []):
        if not isinstance(p, dict):
            continue
        name = p.get("name")
        rel = (p.get("relationship") or "").strip().lower()
        if name and rel in household_relationships:
            members.append(str(name))
    return members


def _build_diet_log() -> str:
    """Read the most recent diet log resource. v0: today only.

    The diet_tracker pipeline writes `resource_diet_log_today.json` and a
    sidecar `_text.md`. We prefer the markdown sidecar for human-readable
    injection; fall back to the JSON if the .md isn't present.
    """
    base = get_resources_dir() / "dayflow_pipeline_outputs"
    md_path = base / "resource_diet_log_today_text.md"
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8").strip()
        return text or _NO_DATA_FMT.format(kind="diet log")
    json_path = base / "resource_diet_log_today.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return json.dumps(data, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.warning("[noticer.context] diet_log JSON parse failed: %s", e)
    return _NO_DATA_FMT.format(kind="diet log")


def _build_recent_chat_clusters(*, now_utc: datetime, limit: int = 12) -> str:
    """Recent chat_cluster pods from the last 48h, headers + one-liner only."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        since = now_utc - timedelta(hours=48)
        results = store.query(kind="chat_cluster", since_utc=since, limit=limit)
    except Exception as e:
        logger.warning("[noticer.context] recent_chat_clusters fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="recent chat clusters")

    if not results:
        return _NO_DATA_FMT.format(kind="recent chat clusters")

    lines: List[str] = []
    for r in results:
        pod_id = getattr(r, "pod_id", "")
        one_liner = getattr(r, "one_liner", "")
        created_at = getattr(r, "created_at", "")
        lines.append(f"- [{pod_id}] {created_at}: {one_liner}")
    return "\n".join(lines)


def _build_recent_friction_signals(
    *,
    now_utc: datetime,
    days: int = 14,
    limit: int = 200,
    primary_user_first_name: Optional[str] = None,
) -> str:
    """Aggregate friction signals from chat_cluster pods over the last N days.

    Reads each pod's metadata.friction_signal (populated by pod_classifier),
    groups by (subject, kind), counts occurrences.

    Subject normalization: 'self' / 'me' / 'I' / 'myself' / the primary
    user's first name all collapse to a single bucket. Without this,
    fatigue mentions split across `self/fatigue_loading` and
    `<first_name>/fatigue_loading` and the noticer's 3+ threshold never
    fires even though the signal is the same person.

    The noticer treats:
      - 1 signal             → noise, log only
      - 2-3 signals          → emerging pattern, low-severity concern
      - 4+ signals           → established pattern, medium-severity concern
      - escalating intensity → severity raised regardless of count
    """
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        since = now_utc - timedelta(days=days)
        results = store.query(kind="chat_cluster", since_utc=since, limit=limit)
    except Exception as e:
        logger.warning("[noticer.context] recent_friction_signals fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="recent friction signals")

    if not results:
        return _NO_DATA_FMT.format(kind="recent friction signals")

    primary = primary_user_first_name or get_required_primary_user_name()
    self_aliases = {"self", "me", "i", "myself", primary.lower()}

    def _normalize_subject(raw: str) -> str:
        if not raw:
            return "unspecified"
        s = raw.strip()
        if s.lower() in self_aliases:
            return primary
        return s

    # Group: (subject, kind) → list of (pod_id, created_at, intensity, quote)
    from collections import defaultdict
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    bare_count = 0
    for r in results:
        meta = getattr(r, "metadata", None) or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            continue
        fs = meta.get("friction_signal")
        if not isinstance(fs, dict):
            bare_count += 1
            continue
        subject = _normalize_subject(fs.get("subject") or "")
        kind = (fs.get("kind") or "unspecified").strip() or "unspecified"
        groups[(subject, kind)].append({
            "pod_id": getattr(r, "pod_id", ""),
            "created_at": str(getattr(r, "created_at", "")),
            "intensity": fs.get("intensity", "?"),
            "quote": (fs.get("quote") or "")[:120],
        })

    if not groups:
        total = len(results)
        return (
            f"(no friction signals detected in last {days} days; "
            f"{total} chat clusters scanned, none carried friction)"
        )

    # Sort groups by count desc, then by most recent occurrence (two-pass:
    # newest-first, then a stable re-sort by count).
    sorted_groups = sorted(
        groups.items(),
        key=lambda kv: max((o["created_at"] for o in kv[1]), default=""),
        reverse=True,
    )
    sorted_groups.sort(key=lambda kv: -len(kv[1]))

    # Split: groups with count >= 2 get full display (these can become
    # patterns). Singletons get a compact tail — they're informational
    # but won't trigger the 3+ threshold; full quotes would just bloat
    # the prompt without changing decisions.
    multi_groups = [(k, v) for k, v in sorted_groups if len(v) >= 2]
    singleton_groups = [(k, v) for k, v in sorted_groups if len(v) == 1]

    lines: List[str] = [
        f"Friction signals over last {days} days "
        f"({sum(len(v) for v in groups.values())} total across {len(groups)} subject/kind groups, "
        f"{bare_count} chat clusters without friction):",
        "",
    ]
    if multi_groups:
        lines.append("Groups with multiple signals (candidates for pattern_drift):")
        for (subject, kind), occurrences in multi_groups:
            occurrences.sort(key=lambda o: o["created_at"], reverse=True)
            count = len(occurrences)
            intensities = [o["intensity"] for o in occurrences]
            max_intensity = (
                "high" if "high" in intensities
                else "medium" if "medium" in intensities
                else "low"
            )
            lines.append(
                f"- subject={subject!r}  kind={kind}  count={count}  max_intensity={max_intensity}"
            )
            # Up to 3 most-recent occurrences with quote + pod ref
            for o in occurrences[:3]:
                quote_preview = o["quote"] if o["quote"] else "(no quote)"
                lines.append(
                    f"    • {o['created_at']}  [{o['intensity']}]  "
                    f"\"{quote_preview}\"  pod={o['pod_id']}"
                )
            if count > 3:
                lines.append(f"    ... and {count - 3} more")
    else:
        lines.append("(no groups with >= 2 signals — nothing crosses the pattern_drift threshold yet)")

    if singleton_groups:
        lines.append("")
        lines.append(
            f"Plus {len(singleton_groups)} count=1 singletons (informational only, won't trigger patterns):"
        )
        # Short per-singleton: subject/kind + most-recent quote
        for (subject, kind), occurrences in singleton_groups[:20]:
            o = occurrences[0]
            quote = (o["quote"] or "(no quote)")[:80]
            lines.append(f"  • {subject!r}/{kind} ({o['intensity']}): \"{quote}\"")
        if len(singleton_groups) > 20:
            lines.append(f"  ... and {len(singleton_groups) - 20} more")
    return "\n".join(lines)


_PASSING_MENTION_MIN_CHARS = 60     # skip fragments ("Yeah. 2006", "Haha jk")
_PASSING_MENTION_MAX_CHARS = 220    # skip multi-paragraph dumps
_PASSING_MENTION_ADMIN_PREFIXES = (
    "trash all", "run task", "delete the", "find out ", "/", "!",
)


def _build_recent_passing_mentions(*, now_utc: datetime, days: int = 4, limit: int = 12) -> str:
    """Short user-authored utterances from the last N days that didn't cluster.

    Tighter than v0: friction-flavored mentions are already in the
    friction_signals aggregate, so this surface mainly catches longer
    informal observations that the classifier didn't pull into a cluster.
    Filters: length 60-220 chars (drops fragments + paragraph dumps);
    skips admin commands; max 12 messages over last 4 days.
    """
    try:
        from sqlalchemy import select, and_
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session

        since = now_utc - timedelta(days=days)
        session = get_session()
        try:
            stmt = (
                select(UnifiedLog2026)
                .where(
                    and_(
                        UnifiedLog2026.timestamp >= since,
                        UnifiedLog2026.role == "user",
                    )
                )
                .order_by(UnifiedLog2026.timestamp.desc())
                .limit(limit * 4)
            )
            rows = session.execute(stmt).scalars().all()
        finally:
            session.close()
    except Exception as e:
        logger.warning("[noticer.context] passing_mentions query failed: %s", e)
        return _NO_DATA_FMT.format(kind="passing mentions")

    out: List[str] = []
    for r in rows:
        msg = (getattr(r, "message", "") or "").strip()
        n = len(msg)
        if n < _PASSING_MENTION_MIN_CHARS or n > _PASSING_MENTION_MAX_CHARS:
            continue
        msg_low = msg.lower()
        if msg_low.startswith(_PASSING_MENTION_ADMIN_PREFIXES):
            continue
        ts = getattr(r, "timestamp", None)
        ts_str = ts.isoformat() if ts else ""
        msg_id = getattr(r, "id", "") or ""
        out.append(f"- [{msg_id}] {ts_str}: {msg}")
        if len(out) >= limit:
            break
    if not out:
        return _NO_DATA_FMT.format(kind="passing mentions")
    return "\n".join(out)


def _build_calendar_today_tomorrow(*, now_local: datetime) -> str:
    """Today + tomorrow's events across all calendars, via the get_calendar_events tool."""
    start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=2)).replace(hour=23, minute=59, second=59)
    return _fetch_calendar_text(start, end, label="today + tomorrow")


def _build_calendar_week_summary(*, now_local: datetime) -> str:
    """Days 2-7 forward from today; the noticer uses this for anticipated needs."""
    start = (now_local + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=6)
    return _fetch_calendar_text(start, end, label="week ahead")


def _build_calendar_30_90d(*, now_local: datetime) -> str:
    """Days 8-90 forward — the horizon Pass B (anticipated_need scouting)
    reads. Trips, school events, family travel, scheduled appointments
    far enough out that prep matters but close enough to act on."""
    start = (now_local + timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=82)
    return _fetch_calendar_text(start, end, label="30-90 day horizon")


def _fetch_calendar_text(start_local: datetime, end_local: datetime, *, label: str) -> str:
    """Invoke the get_calendar_events tool and format the result."""
    try:
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("get_calendar_events")
        if cls is None:
            return _NO_DATA_FMT.format(kind=f"calendar ({label})")
        tool = cls()
        tm = ToolMessage(
            tool_name="get_calendar_events",
            tool_data={
                "arguments": {
                    "start_date": start_local.isoformat(),
                    "end_date": end_local.isoformat(),
                    "single_events": True,
                },
            },
        )
        result = tool.execute(tm)
    except Exception as e:
        logger.warning("[noticer.context] calendar fetch (%s) failed: %s", label, e)
        return _NO_DATA_FMT.format(kind=f"calendar ({label})")

    content = (getattr(result, "content", None) or "").strip()
    if not content:
        return _NO_DATA_FMT.format(kind=f"calendar ({label})")
    return content


def _build_sleep_log() -> str:
    """v0: not wired. Stub. Future: pull from sleep tracking resource / Whoop pipeline."""
    return _NO_DATA_FMT.format(kind="sleep log")


def _build_activity_log() -> str:
    """Read the activity_tracker output if present."""
    base = get_resources_dir() / "dayflow_pipeline_outputs"
    md_path = base / "resource_activity_log_today_text.md"
    if md_path.is_file():
        text = md_path.read_text(encoding="utf-8").strip()
        if text:
            return text
    json_path = base / "resource_activity_log_today.json"
    if json_path.is_file():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return json.dumps(data, indent=2, ensure_ascii=False)
        except Exception:
            pass
    return _NO_DATA_FMT.format(kind="activity log")


def _build_family_roster(members: List[str]) -> str:
    """Simple roster. v0: assume all members are home. Future: derive from
    travel calendar events / KG state."""
    return "\n".join(f"- {m}: home (default — travel not yet tracked here)" for m in members)


def _build_kg_household_digests(members: List[str]) -> str:
    """Per-member KG digest: pull the entity card's description if available."""
    out_parts: List[str] = []
    for member in members:
        digest = _kg_digest_for(member)
        out_parts.append(f"### {member}\n{digest}")
    if not out_parts:
        return _NO_DATA_FMT.format(kind="household KG digests")
    return "\n\n".join(out_parts)


def _kg_digest_for(name: str) -> str:
    """Best-effort KG description for a single household member.

    Direct SQL: find the Entity node with this label, return its description.
    Skips the kg_describe_node tool because that tool wants node UUIDs and
    we have names.
    """
    try:
        from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
        from app.models.base import get_session

        session = get_session()
        try:
            row = (
                session.query(Node)
                .filter(Node.label == name)
                .filter(Node.node_type == "Entity")
                .one_or_none()
            )
        finally:
            session.close()

        if row is None:
            return "(no KG entity card found)"
        description = (row.description or "").strip()
        if not description:
            return f"(node exists, no description; node_id={row.id})"
        return description[:1200]
    except Exception as e:
        logger.warning("[noticer.context] kg_digest for %s failed: %s", name, e)
        return f"(error reading KG: {e})"


def _build_ambient_state_digest(members: List[str]) -> str:
    """v0: not yet derived. Per-tick state would come from chat + observation
    rollup. Stub for now — the noticer's prompt handles missing data."""
    return _NO_DATA_FMT.format(kind="ambient state")


def _build_concerns_register_active() -> str:
    """Read the concerns_register JSON and render active + addressing concerns."""
    path = get_resources_dir() / "subconscious" / "resource_concerns_register.json"
    if not path.is_file():
        return _NO_DATA_FMT.format(kind="concerns register")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[noticer.context] concerns_register parse failed: %s", e)
        return _NO_DATA_FMT.format(kind="concerns register")

    active = data.get("active") or []
    addressing = data.get("addressing") or []
    if not active and not addressing:
        return "(concerns_register is empty — this is a fresh tick or all concerns resolved)"

    lines: List[str] = []
    for c in active:
        lines.append(_render_concern_summary(c, status="active"))
    for c in addressing:
        lines.append(_render_concern_summary(c, status="addressing"))

    # Lifecycle pressure: long-running concerns MUST get a disposition this
    # tick (deterministic detection; the noticer decides what to do).
    from app.assistant.subconscious.persist import compute_pressure
    pressure = compute_pressure(data)
    pressured = {c.get("concern_id"): "reinforced many times since last decision"
                 for c in pressure["needs_disposition"]}
    for c in pressure["addressing_stale"]:
        cid = c.get("concern_id")
        why = f"stale in 'addressing' since {str(c.get('addressing_since_utc') or '')[:10]}"
        pressured[cid] = f"{pressured[cid]} AND {why}" if cid in pressured else why
    if pressured:
        by_id = {c.get("concern_id"): c for c in [*active, *addressing]}
        lines.append("## CONCERNS UNDER PRESSURE — disposition REQUIRED this tick")
        lines.append(
            "Each concern below has run long enough that continuing to reinforce "
            "it is no longer a decision-free default. Emit exactly one "
            "concern_dispositions entry per id: accept_chronic (real but "
            "long-term — archive with a summary), re_escalate (handling stalled "
            "or it got worse — push back to active with high urgency), or "
            "keep_active (justify WHY longer tracking is right)."
        )
        for cid, why in pressured.items():
            c = by_id.get(cid) or {}
            count = c.get("reinforcement_count")
            lines.append(
                f"- {cid} — {c.get('title', '(no title)')} "
                f"[{why}; reinforcements={count if count is not None else '?'}]"
            )
    return "\n\n".join(lines)


def _build_question_mailbox() -> str:
    """Captured answers to noticer questions + stale unanswered asks.

    Every listed item demands a question_outcomes entry from the noticer:
    answered questions get processed into concern updates; stale ones get
    their stated default applied (outcome expired)."""
    try:
        from app.assistant.pending_questions import get_for_noticer_processing
        mailbox = get_for_noticer_processing()
    except Exception as e:
        logger.warning("[noticer.context] question mailbox fetch failed: %s", e)
        return "(question mailbox unavailable this tick)"

    answered = mailbox.get("answered") or []
    stale = mailbox.get("stale_asked") or []
    if not answered and not stale:
        return "(no answers waiting, no stale questions)"

    lines: List[str] = []
    if answered:
        lines.append("### ANSWERS RECEIVED — process each into its concern")
        for q in answered:
            lines.append(
                f"- question_id={q.id}\n"
                f"  asked: {q.question_text}\n"
                f"  USER'S ANSWER: {q.answer_text}\n"
                f"  related_concern_id: {q.related_concern_id or '(none)'}"
            )
    if stale:
        lines.append("### UNANSWERED >48h — apply the stated default (outcome: expired)")
        for q in stale:
            lines.append(
                f"- question_id={q.id}\n"
                f"  asked: {q.question_text}\n"
                f"  asked_at: {q.asked_at}\n"
                f"  related_concern_id: {q.related_concern_id or '(none)'}"
            )
    return "\n".join(lines)


def _render_concern_summary(c: Dict[str, Any], *, status: str) -> str:
    """One-block readable summary for a concern in the register."""
    parts = [
        f"[{status}] {c.get('concern_id', '?')} — {c.get('title', '(no title)')}",
        f"  subject: {c.get('subject') or 'household'}",
        f"  kind: {c.get('kind')} | severity: {c.get('severity')} | horizon: {c.get('horizon')}",
        f"  domain_tags: {', '.join(c.get('domain_tags') or [])}",
        f"  addressable_by: {', '.join(c.get('addressable_by') or [])}",
        f"  first_observed: {c.get('first_observed')}",
        f"  notes: {(c.get('notes') or '')[:300]}",
    ]
    return "\n".join(parts)


def _build_exploration_outcomes(*, now_utc: datetime, days: int = 30) -> str:
    """Recent exploration_attempt pods + their outcomes. Empty on first runs."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        since = now_utc - timedelta(days=days)
        results = store.query(tags=["exploration_attempt"], since_utc=since, limit=40)
    except Exception as e:
        logger.warning("[noticer.context] exploration_outcomes fetch failed: %s", e)
        return _NO_DATA_FMT.format(kind="exploration outcomes")

    if not results:
        return "(no exploration attempts logged yet)"

    lines: List[str] = []
    for r in results:
        pod_id = getattr(r, "pod_id", "")
        one_liner = getattr(r, "one_liner", "")
        lines.append(f"- [{pod_id}] {one_liner}")
    return "\n".join(lines)


_DAYFLOW_INTERNAL_ID_PREFIXES = ("action_log:", "action_dispatch:", "action_result:")
_DAYFLOW_BOOKKEEPING_STATES = {"artifact"}  # intermediate chat snapshots, not signal


def _build_dayflow_recent(*, now_utc: datetime, hours: int = 48, limit: int = 10) -> str:
    """Recent dayflow_item entries from unified_log — filtered to signal-worthy rows.

    Filters out internal bookkeeping:
    - action_log:* (except "Result: ..." outcome lines) / action_dispatch:* /
      action_result:* — dayflow's internal mechanism. Outcome lines are KEPT so the
      noticer can see what dayflow accomplished and move handled concerns to addressing.
    - state=artifact — intermediate chat snapshots that already appear elsewhere
      in passing_mentions / chat_clusters.

    Keeps: items with numeric short_ids and terminal/active states
    (new, dispatched, suppressed, closed, waiting, watching, etc.).
    """
    try:
        from sqlalchemy import select, and_
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session

        since = now_utc - timedelta(hours=hours)
        session = get_session()
        try:
            stmt = (
                select(UnifiedLog2026)
                .where(
                    and_(
                        UnifiedLog2026.timestamp >= since,
                        UnifiedLog2026.source == "dayflow_item",
                    )
                )
                .order_by(UnifiedLog2026.timestamp.desc())
                .limit(limit * 4)
            )
            rows = session.execute(stmt).scalars().all()
        finally:
            session.close()
    except Exception as e:
        logger.warning("[noticer.context] dayflow_recent query failed: %s", e)
        return _NO_DATA_FMT.format(kind="recent dayflow items")

    if not rows:
        return "(no recent dayflow items)"

    out: List[str] = []
    seen_short_ids: set = set()
    for r in rows:
        msg = (getattr(r, "message", "") or "").strip()
        meta = getattr(r, "metadata_json", None) or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        state = meta.get("state", "?")
        short_id = str(meta.get("short_id") or meta.get("item_id") or "")

        # Skip dayflow's internal bookkeeping rows — EXCEPT outcome lines ("Result: ...") from
        # action_log, which tell the noticer what dayflow actually ACCOMPLISHED. Seeing these lets
        # it move an already-handled concern to `addressing` instead of reinforcing it as ignored.
        is_outcome = short_id.startswith("action_log:") and msg.startswith("Result:")
        if short_id.startswith(_DAYFLOW_INTERNAL_ID_PREFIXES) and not is_outcome:
            continue
        if state in _DAYFLOW_BOOKKEEPING_STATES and not is_outcome:
            continue
        # Dedup by short_id — keep only the most-recent row per item
        if short_id and short_id in seen_short_ids:
            continue
        if short_id:
            seen_short_ids.add(short_id)

        ts = getattr(r, "timestamp", None)
        ts_str = ts.isoformat() if ts else ""
        display_id = short_id or "?"
        out.append(f"- {ts_str} [{display_id}] state={state}: {msg[:200]}")
        if len(out) >= limit:
            break

    if not out:
        return "(no signal-worthy dayflow items in window)"
    return "\n".join(out)


def _build_watchlist_summary() -> str:
    """Pass B input. Reads the user-curated watchlist markdown verbatim.

    The watchlist lists things Jukka wants surfaced when external signals
    align: restaurants to try, books to track, family members to keep
    tabs on, trips planned, etc. The noticer scans this against the
    week's other context (calendar, KG, chat) for matches.

    Returns the file's content if present, or a clearly-marked fallback
    so Pass B knows nothing's configured."""
    path = get_resources_dir() / "subconscious" / "resource_subconscious_watchlist.md"
    if not path.is_file():
        return "(no watchlist file — Pass B's external scouting has nothing curated to watch)"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("[noticer.context] watchlist read failed: %s", e)
        return "(error reading watchlist)"
    if not text:
        return "(watchlist file is empty — Pass B should focus on anticipated_need scanning)"
    return text


def _build_family_graph_digest() -> str:
    """Pass B input. Reads KG entity cards for important_people who are
    NOT part of the household (extended family, close friends). Surfaces:
    - upcoming birthdays within 90 days (one-shot date math)
    - per-person KG description (relationship, recent state, notable facts)
    - flagged "recent mention" if their name appeared in chat clusters
      in the last 14 days

    This is the v0 of family-graph projection (Phase 4b). When the
    underlying KG grows richer state-change tracking, this builder will
    naturally surface more.

    Returns a markdown block ready for direct interpolation. Empty/missing
    data falls back gracefully — the noticer's prompt handles partial
    context."""
    from datetime import date, timedelta as _td

    try:
        user_data_path = get_resources_dir() / "user" / "resource_user_data.json"
        if not user_data_path.is_file():
            return "(no resource_user_data.json — no family graph to project)"
        user_data = json.loads(user_data_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[noticer.context] user_data read failed for family_graph: %s", e)
        return "(error reading user_data)"

    important_people = user_data.get("important_people") or []
    if not important_people:
        return "(no important_people in user_data — family graph is empty)"

    # Household = self + spouse/partner + children (already covered by
    # kg_household_digests). Family graph = the REST.
    household_relationships = {
        "wife", "husband", "spouse", "partner",
        "son", "daughter", "child", "kid",
    }
    extended = [
        p for p in important_people
        if isinstance(p, dict)
        and (p.get("relationship") or "").strip().lower() not in household_relationships
    ]
    if not extended:
        return "(no extended family/friends in important_people — only household members listed)"

    today = date.today()
    horizon = today + _td(days=90)

    sections: List[str] = []
    for person in extended:
        name = (person.get("name") or "").strip()
        if not name:
            continue
        relationship = (person.get("relationship") or "").strip() or "unspecified"
        bd = person.get("birthdate")
        section = [f"### {name} ({relationship})"]

        # Upcoming birthday within 90 days
        if bd:
            try:
                original = datetime.strptime(str(bd), "%Y-%m-%d").date()
                candidate = original.replace(year=today.year)
                if candidate < today:
                    candidate = candidate.replace(year=today.year + 1)
                if candidate <= horizon:
                    days_away = (candidate - today).days
                    section.append(
                        f"- **Upcoming birthday:** {candidate.isoformat()} ({days_away}d away)"
                    )
            except (ValueError, TypeError):
                pass

        # KG digest — reuses the household-digest helper
        digest = _kg_digest_for(name).strip()
        if digest and not digest.startswith("(no KG"):
            # Truncate aggressively — this is a digest, not a full card
            short = digest[:600]
            if len(digest) > 600:
                short += " …"
            section.append(f"- KG snapshot: {short}")

        # Recent chat-cluster mention check
        try:
            from app.assistant.pod_store.pod_store import PodStore
            store = PodStore()
            hits = store.query(kind="chat_cluster", query=name, since="14d", limit=3)
            if hits:
                section.append(f"- **Recently mentioned** in {len(hits)} chat cluster(s) over last 14 days:")
                for h in hits:
                    snippet = (h.one_liner or "").replace("\n", " ")[:160]
                    section.append(f"  - {h.pod_id}: {snippet}")
        except Exception as e:
            logger.debug("[noticer.context] family_graph mention check failed for %s: %s", name, e)

        if len(section) > 1:  # has more than just the header
            sections.append("\n".join(section))

    if not sections:
        return (
            "(family graph has people but no current signals — no upcoming "
            "birthdays, no KG descriptions, no recent mentions in chat)"
        )
    return "\n\n".join(sections)


def _build_recurring_obligations() -> str:
    """Pass B input. Reads the recurring-obligations resource verbatim.

    Lists predictable due-date items (annual physical, insurance renewal,
    car service, etc.) with cadence + next_due + lead_time. The noticer
    surfaces an anticipated_need when today is within lead_time of next_due.

    Returns the file's content if present, or a clearly-marked fallback."""
    path = get_resources_dir() / "subconscious" / "resource_recurring_obligations.md"
    if not path.is_file():
        return "(no recurring obligations file — Pass B's anticipated_need scanning has nothing curated)"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.warning("[noticer.context] recurring obligations read failed: %s", e)
        return "(error reading recurring obligations)"
    if not text:
        return "(recurring obligations file is empty)"
    return text


def build_weekly_schedule_block() -> str:
    """Render the latest plan.weekly_schedule pod for proposer context.

    The scheduler_arbiter is the sole producer of plan.weekly_schedule pods.
    Every proposer (meal, wellness, romantic) reads this block and treats
    its `is_anchor` items as LOCKED constraints. Non-anchor items are
    scheduled-but-flex (the proposer can move them if context warrants).

    Returns a multi-line block ready for direct interpolation into a
    proposer's user.j2. Falls back gracefully when no schedule has been
    generated yet (first run / arbiter never invoked)."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        store = PodStore()
        results = store.query(kind="plan", tags=["weekly_schedule"], since="10d", limit=1)
    except Exception as e:
        logger.warning("[context_builder] weekly_schedule fetch failed: %s", e)
        return "(no weekly schedule readable — plan independently and let scheduler_arbiter resolve)"

    if not results:
        return "(no weekly schedule exists yet — first arbiter run hasn't happened. Plan independently; arbiter resolves conflicts after.)"

    latest = results[0]
    return f"{latest.one_liner}\n\n{latest.body}"
