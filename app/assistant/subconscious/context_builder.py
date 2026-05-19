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
from app.assistant.utils.path_utils import get_repo_root
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

    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "trigger_mode": trigger_mode,
        "diet_log_7d": _build_diet_log(),
        "recent_chat_clusters": _build_recent_chat_clusters(now_utc=now_utc),
        "recent_passing_mentions": _build_recent_passing_mentions(now_utc=now_utc),
        "calendar_today_tomorrow": _build_calendar_today_tomorrow(now_local=now_local),
        "calendar_week_summary": _build_calendar_week_summary(now_local=now_local),
        "sleep_log_7d": _build_sleep_log(),
        "activity_log_7d": _build_activity_log(),
        "family_roster": _build_family_roster(household_members),
        "kg_household_digests": _build_kg_household_digests(household_members),
        "ambient_state_digest": _build_ambient_state_digest(household_members),
        "concerns_register_active": _build_concerns_register_active(),
        "exploration_outcomes_30d": _build_exploration_outcomes(now_utc=now_utc),
        "dayflow_recent": _build_dayflow_recent(now_utc=now_utc),
        "watchlist_summary": _build_watchlist_summary(),
    }


# ── individual section builders ──────────────────────────────────────────


def _resolve_household_members() -> List[str]:
    """Read user_data → first_name + important_people (close relations) to
    identify the household roster.

    Future: refine with KG cohabits-with / lives-with / is-family-of edges.
    """
    try:
        user_data_path = get_repo_root() / "resources" / "user" / "resource_user_data.json"
        if user_data_path.is_file():
            data = json.loads(user_data_path.read_text(encoding="utf-8"))
            first_name = data.get("first_name") or "User"
            members = [first_name]
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
    except Exception as e:
        logger.warning("[noticer.context] household_members resolution failed: %s", e)
    return ["Jukka"]


def _build_diet_log() -> str:
    """Read the most recent diet log resource. v0: today only.

    The diet_tracker pipeline writes `resource_diet_log_today.json` and a
    sidecar `_text.md`. We prefer the markdown sidecar for human-readable
    injection; fall back to the JSON if the .md isn't present.
    """
    base = get_repo_root() / "resources" / "dayflow_pipeline_outputs"
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


def _build_recent_passing_mentions(*, now_utc: datetime, days: int = 7, limit: int = 30) -> str:
    """Short user-authored utterances from the last N days that didn't cluster.

    v0: query unified_log_2026 for short user messages, ordered by recency.
    The "didn't cluster" filter is approximated by length < 200 chars; the
    real filter would join against chat_cluster membership but that table
    isn't queryable here without more plumbing.
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
                .limit(limit * 2)
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
        if not msg or len(msg) > 200:
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
    base = get_repo_root() / "resources" / "dayflow_pipeline_outputs"
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
    path = get_repo_root() / "resources" / "subconscious" / "resource_concerns_register.json"
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
    return "\n\n".join(lines)


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


def _build_dayflow_recent(*, now_utc: datetime, hours: int = 48, limit: int = 25) -> str:
    """Recent dayflow_item entries from unified_log."""
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
                .limit(limit)
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
    for r in rows:
        ts = getattr(r, "timestamp", None)
        ts_str = ts.isoformat() if ts else ""
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
        short_id = meta.get("short_id") or meta.get("item_id") or "?"
        out.append(f"- {ts_str} [{short_id}] state={state}: {msg[:200]}")
    return "\n".join(out)


def _build_watchlist_summary() -> str:
    """Pass B only. v0: not yet wired. Will read resource_subconscious_watchlist.md later."""
    return "(watchlist not yet configured — Pass B should be skipped this tick)"
