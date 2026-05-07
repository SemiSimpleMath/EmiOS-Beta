"""
Stage 1: Per-message entity resolution.

Reads chat-eligible rows from unified_log_2026 directly (no intermediate
filter table) and emits one kg_resolved_message row per message, keyed by
unified_log_id.

Walks day-bounded chronologically. For each day, processes unresolved
messages in batches of 10 with up to 10 already-resolved prior-same-day
messages as context. Each message is resolved exactly once.

Agent: knowledge_graph_add::entity_resolver (gpt-5.4-mini).

Chat-eligibility filter (matches the legacy FilterMessagesStep):
    source ∈ {chat, room_slack, room_sms, room_ui}
    role ∈ {user, assistant}
    non-empty message
    room_id ∈ {master_room} or null

START_LOCAL_DATE caps how far back the resolver will go. Set to today's
local date when a clean cutover is desired (avoid LLM cost on historical
backlog). Set to None to backfill the entire history.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_pipeline_models import KGResolvedMessage
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_timezone
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


# Sources eligible for KG ingestion. The user-authored prose-resolution
# source IS auto-ingested (the user typed it to answer a question — it's
# trusted input, conceptually identical to a chat message).
#   kg_maintenance_resolution — prose-resolution answers from the cluster
#                               resolver UI; user-authored.
# wiki_inference is NOT here — agent-inferred sentences route to the
# wiki-review queue (kg_maintenance_finding type='wiki_inferred_edge')
# for user approval before any ingestion. They will eventually flow
# through a wiki-specific pipeline (TBD), not this chat path.
CHAT_SOURCES = {
    "chat", "room_slack", "room_sms", "room_ui",
    "kg_maintenance_resolution",
}
CHAT_ROLES = {"user", "assistant"}
ALLOWED_ROOMS = {"master_room"}

BATCH_NEW_SIZE = 10
BATCH_CONTEXT_SIZE = 10
RESOLVER_AGENT_NAME = "knowledge_graph_add::entity_resolver"
RESOLVER_VERSION = "kg_pipeline_v1"


class ResolveMessagesStep:
    name = "resolve_messages"
    MAX_BATCHES_PER_CYCLE = 40

    # Local-date cutoff (YYYY-MM-DD). Messages with local_date < this are ignored.
    # Set to None to process the entire history (backfill mode).
    START_LOCAL_DATE: Optional[str] = "2026-04-25"

    # Sharper UTC-precise cutoff used to skip a one-time recovery backlog
    # (2026-04-29: ~13K source-clobbered chat rows revived after the
    # save_to_unified_db identity-fields fix). Anything older than this
    # timestamp is invisible to the resolver — no LLM cost, no segmenter
    # work. Set to None to revert to the START_LOCAL_DATE-only cutoff.
    START_TIMESTAMP_UTC: Optional[str] = "2026-04-30T03:00:00+00:00"

    @staticmethod
    def _local_date(ts: datetime) -> date:
        tz = get_local_timezone()
        if ts.tzinfo is None:
            from datetime import timezone as _tz
            ts = ts.replace(tzinfo=_tz.utc)
        return ts.astimezone(tz).date()

    def _eligible_filter(self, query):
        """Apply the chat-eligibility filter to a query on UnifiedLog2026."""
        return query.filter(
            UnifiedLog2026.source.in_(CHAT_SOURCES),
            UnifiedLog2026.role.in_(CHAT_ROLES),
            UnifiedLog2026.message.isnot(None),
            UnifiedLog2026.message != "",
            or_(
                UnifiedLog2026.room_id.is_(None),
                UnifiedLog2026.room_id == "",
                UnifiedLog2026.room_id.in_(ALLOWED_ROOMS),
            ),
        )

    def _resolved_cutoff_utc(self) -> Optional[datetime]:
        """Return the effective UTC cutoff timestamp (the later of
        START_LOCAL_DATE-midnight and START_TIMESTAMP_UTC)."""
        from datetime import timezone as _tz
        cutoffs: list[datetime] = []
        if self.START_LOCAL_DATE:
            tz = get_local_timezone()
            start_local = datetime.combine(
                date.fromisoformat(self.START_LOCAL_DATE),
                datetime.min.time(),
                tzinfo=tz,
            )
            cutoffs.append(start_local.astimezone(_tz.utc))
        if self.START_TIMESTAMP_UTC:
            ts = datetime.fromisoformat(self.START_TIMESTAMP_UTC)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=_tz.utc)
            cutoffs.append(ts.astimezone(_tz.utc))
        if not cutoffs:
            return None
        return max(cutoffs)

    def pending_count(self) -> int:
        from sqlalchemy import func as sqlfunc
        with get_db_manager().read_session() as session:
            resolved_ids = session.query(KGResolvedMessage.unified_log_id).subquery().select()
            q = self._eligible_filter(
                session.query(sqlfunc.count(UnifiedLog2026.id))
                .filter(~UnifiedLog2026.id.in_(resolved_ids))
            )
            cutoff = self._resolved_cutoff_utc()
            if cutoff is not None:
                q = q.filter(UnifiedLog2026.timestamp >= cutoff)
            return int(q.scalar() or 0)

    def _find_next_unresolved_day(self) -> Optional[date]:
        with get_db_manager().read_session() as session:
            resolved_ids = session.query(KGResolvedMessage.unified_log_id).subquery().select()
            q = self._eligible_filter(
                session.query(UnifiedLog2026.timestamp)
                .filter(~UnifiedLog2026.id.in_(resolved_ids))
            ).order_by(UnifiedLog2026.timestamp.asc())

            cutoff = self._resolved_cutoff_utc()
            if cutoff is not None:
                q = q.filter(UnifiedLog2026.timestamp >= cutoff)

            row = q.first()
            if row is None:
                return None
            return self._local_date(row[0])

    def _load_day_messages(self, local_day: date) -> List[Dict]:
        tz = get_local_timezone()
        start_local = datetime.combine(local_day, datetime.min.time(), tzinfo=tz)
        end_local = start_local + timedelta(days=1)
        from datetime import timezone as _tz
        start_utc = start_local.astimezone(_tz.utc)
        end_utc = end_local.astimezone(_tz.utc)

        # Honor the precise cutoff so messages earlier than START_TIMESTAMP_UTC
        # on the chosen day are skipped — otherwise picking a day still loads
        # everything from local-midnight onward.
        cutoff = self._resolved_cutoff_utc()
        if cutoff is not None and cutoff > start_utc:
            start_utc = cutoff

        with get_db_manager().read_session() as session:
            rows = self._eligible_filter(
                session.query(UnifiedLog2026)
                .filter(UnifiedLog2026.timestamp >= start_utc)
                .filter(UnifiedLog2026.timestamp < end_utc)
            ).order_by(UnifiedLog2026.timestamp.asc(), UnifiedLog2026.id.asc()).all()

            existing_set = set(
                rid for (rid,) in session.query(KGResolvedMessage.unified_log_id)
                .filter(KGResolvedMessage.unified_log_id.in_([r.id for r in rows]))
                .all()
            )
            return [
                {
                    "unified_log_id": r.id,
                    "timestamp": r.timestamp,
                    "role": r.role,
                    "speaker_name": r.speaker_name,
                    "raw_text": r.message,
                    "resolved_text": None if r.id not in existing_set else "ALREADY_RESOLVED",
                }
                for r in rows
            ]

    @staticmethod
    def _plan_next_batch(day_messages: List[Dict]) -> Optional[Tuple[List[Dict], List[Dict]]]:
        n = len(day_messages)
        first_new_idx = None
        for i in range(n):
            if day_messages[i]["resolved_text"] is None:
                first_new_idx = i
                break
        if first_new_idx is None:
            return None
        new: List[Dict] = []
        for k in range(first_new_idx, n):
            if len(new) >= BATCH_NEW_SIZE:
                break
            if day_messages[k]["resolved_text"] is None:
                new.append(day_messages[k])
            else:
                break
        context = [
            day_messages[k]
            for k in range(0, first_new_idx)
            if day_messages[k]["resolved_text"] is not None
        ][-BATCH_CONTEXT_SIZE:]
        return context, new

    @staticmethod
    def _safe_id(uid: str) -> str:
        return uid.replace("-", "_").replace(":", "_")

    def _line(self, msg: Dict, resolved: bool = False) -> str:
        role = msg["role"] or "user"
        spk = (msg.get("speaker_name") or role or "user").strip() or role
        text = msg["raw_text"] if not resolved else (msg.get("resolved_text") or msg["raw_text"])
        return f"[msg:{self._safe_id(msg['unified_log_id'])}|{role}] {spk}: {(text or '').strip()}"

    def _run_resolver_on_batch(self, context: List[Dict], new: List[Dict]) -> Dict:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.agents.knowledge_graph_add.entity_resolver.agent_form import build_dynamic_form
        from app.assistant.pipelines.scope_policy import build_pipeline_scope_context

        ctx_block = "\n".join(self._line(m, resolved=True) for m in context)
        new_tagged = "\n".join(self._line(m, resolved=False) for m in new)

        agent = DI.agent_factory.create_agent(RESOLVER_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"Failed to create resolver agent: {RESOLVER_AGENT_NAME}")
        msg_ids = [self._safe_id(m["unified_log_id"]) for m in new]
        agent.config["structured_output"] = build_dynamic_form(msg_ids)

        scope = build_pipeline_scope_context(
            pipeline_id="kg_pipeline", actor_id="resolve_messages",
        )
        agent_input = {"text": new_tagged}
        if ctx_block:
            agent_input["previous_context"] = ctx_block

        result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = result.data if result and hasattr(result, "data") else {}
        return data if isinstance(data, dict) else {}

    def _write_batch(self, new: List[Dict], resolver_data: Dict) -> int:
        written = 0
        with get_db_manager().transaction(op="resolve.write_batch") as session:
            for m in new:
                safe_id = self._safe_id(m["unified_log_id"])
                entry = resolver_data.get(safe_id)
                resolved_text = None
                if isinstance(entry, dict):
                    resolved_text = str(entry.get("resolved_text") or "").strip() or None
                elif hasattr(entry, "resolved_text"):
                    resolved_text = (str(entry.resolved_text or "").strip() or None)
                if resolved_text is None:
                    # Resolver silent — fall back to raw_text so downstream sees a value.
                    resolved_text = m["raw_text"]

                session.add(
                    KGResolvedMessage(
                        unified_log_id=m["unified_log_id"],
                        unified_timestamp=m["timestamp"],
                        resolved_text=resolved_text,
                        resolved_entities=None,
                        resolver_version=RESOLVER_VERSION,
                        resolved_at=datetime.utcnow(),
                    )
                )
                written += 1
        return written

    def run_one_cycle(self, ctx) -> int:
        batches_done = 0
        messages_resolved = 0
        days_seen: set = set()

        while batches_done < self.MAX_BATCHES_PER_CYCLE:
            next_day = self._find_next_unresolved_day()
            if next_day is None:
                break
            days_seen.add(next_day)
            day_messages = self._load_day_messages(next_day)
            if not day_messages:
                break
            batch = self._plan_next_batch(day_messages)
            if batch is None:
                continue
            context, new = batch
            logger.info(
                "[kg_pipeline.resolve] day=%s ctx=%d new=%d first_ts=%s",
                next_day, len(context), len(new),
                new[0]["timestamp"].isoformat() if new else "-",
            )
            resolver_data = self._run_resolver_on_batch(context, new)
            written = self._write_batch(new, resolver_data)
            messages_resolved += written
            batches_done += 1

        if messages_resolved:
            logger.info(
                "[kg_pipeline.resolve] cycle done: days=%d batches=%d messages=%d",
                len(days_seen), batches_done, messages_resolved,
            )
        return messages_resolved
