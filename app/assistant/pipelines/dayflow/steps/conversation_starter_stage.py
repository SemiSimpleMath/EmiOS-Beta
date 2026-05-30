"""
Conversation Starter Stage

Purpose:
- Initiate light conversation with the user a few times per day.
- Uses heavy context (day artifacts + week calendar) to produce a simple 1-2 sentence opener.
- Avoids repeating previous starters by maintaining a small pointer resource + per-day JSONL log.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.dayflow.utils.room_scope import resolve_room_scope
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Min-interval map for the user-controlled frequency setting (assistant_core
# JSON, edited in /settings/assistant UI). The pipeline scheduler's own
# `min_interval_seconds` floor still applies on top of this; the goal here
# is to let the user dial Emi's chattiness from "never" to "frequent" without
# touching the dayflow config.
_MIN_INTERVAL_FOR_FREQUENCY: Dict[str, int] = {
    "off":      10 ** 12,   # effectively never (also short-circuited above)
    "rare":     3 * 3600,   # 3 hours
    "regular":  20 * 60,    # 20 minutes
    "frequent": 5 * 60,     # 5 minutes
}
_DEFAULT_FREQUENCY = "regular"


def _load_conversation_starter_frequency() -> str:
    """Read frequency from resources/assistant/assistant_core.json. Falls back
    to 'regular' if the file is missing, unparseable, or has no value set —
    matches the existing default for users who haven't visited the settings
    page since this feature shipped.
    """
    try:
        from app.assistant.utils.path_utils import get_resources_dir
        core_path = get_resources_dir() / "assistant" / "assistant_core.json"
        if not core_path.exists():
            return _DEFAULT_FREQUENCY
        data = json.loads(core_path.read_text(encoding="utf-8"))
        cs = data.get("conversation_starter") if isinstance(data, dict) else None
        if not isinstance(cs, dict):
            return _DEFAULT_FREQUENCY
        freq = (cs.get("frequency") or "").strip().lower()
        return freq if freq in _MIN_INTERVAL_FOR_FREQUENCY else _DEFAULT_FREQUENCY
    except Exception:
        logger.debug("Could not load conversation_starter.frequency", exc_info=True)
        return _DEFAULT_FREQUENCY


def _extract_first_question(memo_text: str) -> str:
    """
    Pull the first bullet question from a context_activation memo.

    The memo format produced by context_memo.distil_memo() is:

        ## Situation Summary
        ...

        ## Suggested Questions
        - Question one?
        - Question two?
        ...

    Returns the first bullet item text, or "" if none found.
    """
    in_questions = False
    for line in memo_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("## suggested question"):
            in_questions = True
            continue
        if in_questions:
            if stripped.startswith("##"):
                break
            if stripped.startswith("-") or stripped.startswith("*"):
                question = stripped.lstrip("-*").strip()
                if question:
                    return question
    return ""


def _normalize_text(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _starter_id(text: str) -> str:
    norm = _normalize_text(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _parse_hhmm(hhmm: str) -> Optional[Tuple[int, int]]:
    if not isinstance(hhmm, str):
        return None
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", hhmm)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    if h < 0 or h > 23 or mi < 0 or mi > 59:
        return None
    return h, mi


def _in_time_range(now_local: datetime, spec: str) -> bool:
    """
    spec: "HH:MM-HH:MM" (local). Supports wrapping over midnight.
    """
    if not isinstance(spec, str) or "-" not in spec:
        return False
    a, b = [p.strip() for p in spec.split("-", 1)]
    pa = _parse_hhmm(a)
    pb = _parse_hhmm(b)
    if not pa or not pb:
        return False
    sh, sm = pa
    eh, em = pb
    start = now_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now_local.replace(hour=eh, minute=em, second=0, microsecond=0)
    if start <= end:
        return start <= now_local <= end
    # Wraps midnight
    return now_local >= start or now_local <= end


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_json_optional(path: Path) -> Any:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


@dataclass
class _GateDecision:
    ok: bool
    reason: str


class ConversationStarterStep(BaseStep):
    step_id: str = "conversation_starter"

    def _build_pipeline_scope_context(self, agent_input: dict):
        history_scope = agent_input.get("history_scope") if isinstance(agent_input, dict) else {}
        room_id = str(history_scope.get("room_id") or "").strip() if isinstance(history_scope, dict) else ""
        room_surface = str(history_scope.get("room_surface") or "").strip() if isinstance(history_scope, dict) else ""
        return load_scope_for_source(kind="pipeline", source_id="dayflow", actor_id=f"{self.step_id}_runner", identity_overrides={"room_id": room_id or None, "surface": (room_surface or None) or "pipeline"})

    def _boundary_date_local(self, ctx: StepContext) -> str:
        return str(ctx.state.get("boundary_date_local") or ctx.now_local.strftime("%Y-%m-%d"))

    def _day_dir(self, ctx: StepContext, boundary_date_local: str) -> Path:
        year = boundary_date_local[:4]
        month = boundary_date_local[5:7]
        repo_root = Path(ctx.resources_dir).parent
        return repo_root / "day_context" / year / month / boundary_date_local

    def _latest_pointer_path(self, ctx: StepContext) -> Path:
        return Path(ctx.resources_dir) / "resource_conversation_starters_latest.json"

    def _load_latest_pointer(self, ctx: StepContext) -> Dict[str, Any]:
        data = _read_json_optional(self._latest_pointer_path(ctx))
        return data if isinstance(data, dict) else {}

    def _save_latest_pointer(self, ctx: StepContext, payload: Dict[str, Any]) -> None:
        ctx.write_resource("resource_conversation_starters_latest.json", payload)

    def _calendar_veto(self, ctx: StepContext, stage_cfg: Dict[str, Any]) -> _GateDecision:
        veto = stage_cfg.get("calendar_veto", {}) if isinstance(stage_cfg, dict) else {}
        if not isinstance(veto, dict):
            return _GateDecision(True, "calendar_veto disabled")
        starts_within = int(veto.get("veto_if_event_starts_within_minutes", 10))
        meeting_now = bool(veto.get("veto_if_meeting_now", True))

        try:
            from app.assistant.pipelines.dayflow.utils.context_sources import _load_calendar_events  # type: ignore

            now = datetime.now(timezone.utc)
            for ev in _load_calendar_events() or []:
                start = ev.get("start_utc")
                end = ev.get("end_utc")
                if not isinstance(start, datetime):
                    continue
                # Meeting now
                if meeting_now and end and isinstance(end, datetime) and start <= now <= end:
                    summary = str(ev.get("summary") or "")
                    return _GateDecision(False, f"meeting_now:{summary}")
                # Event soon
                if start > now:
                    mins = (start - now).total_seconds() / 60.0
                    if 0 <= mins <= float(starts_within):
                        summary = str(ev.get("summary") or "")
                        return _GateDecision(False, f"event_soon:{int(mins)}m:{summary}")
        except Exception:
            logger.debug("calendar_veto check failed; allowing conversation starter", exc_info=True)
            return _GateDecision(True, "calendar_veto error (allow)")

        return _GateDecision(True, "calendar ok")

    def _presence_veto(self, ctx: StepContext, stage_cfg: Dict[str, Any]) -> _GateDecision:
        veto = stage_cfg.get("presence_veto", {}) if isinstance(stage_cfg, dict) else {}
        if not isinstance(veto, dict):
            return _GateDecision(True, "presence_veto disabled")
        if not (veto.get("veto_if_afk", True) or veto.get("veto_if_potentially_afk", True)):
            return _GateDecision(True, "presence_veto disabled")

        afk = ctx.read_resource("resource_afk_statistics_output.json") or {}
        if isinstance(afk, dict):
            if veto.get("veto_if_afk", True) and bool(afk.get("is_afk", False)):
                return _GateDecision(False, "user_afk")
            if veto.get("veto_if_potentially_afk", True) and bool(afk.get("is_potentially_afk", False)):
                return _GateDecision(False, "user_potentially_afk")
        return _GateDecision(True, "presence ok")

    def _quiet_hours_veto(self, ctx: StepContext, stage_cfg: Dict[str, Any]) -> _GateDecision:
        quiet = stage_cfg.get("quiet_hours_local", []) if isinstance(stage_cfg, dict) else []
        if not quiet:
            return _GateDecision(True, "quiet_hours disabled")
        if isinstance(quiet, str):
            quiet = [quiet]
        if not isinstance(quiet, list):
            return _GateDecision(True, "quiet_hours invalid (allow)")
        for spec in quiet:
            if isinstance(spec, str) and _in_time_range(ctx.now_local, spec):
                return _GateDecision(False, f"quiet_hours:{spec}")
        return _GateDecision(True, "quiet_hours ok")

    def _rate_limit_veto(self, ctx: StepContext, stage_cfg: Dict[str, Any], boundary_date_local: str) -> _GateDecision:
        max_per_day = int(stage_cfg.get("max_per_day", 3) if isinstance(stage_cfg, dict) else 3)
        min_interval_minutes = int(stage_cfg.get("min_interval_minutes", 180) if isinstance(stage_cfg, dict) else 180)

        pointer = self._load_latest_pointer(ctx)
        last_sent_utc = pointer.get("last_sent_utc")
        last_boundary = pointer.get("boundary_date_local")
        sent_today = int(pointer.get("sent_today", 0) or 0) if last_boundary == boundary_date_local else 0

        if max_per_day > 0 and sent_today >= max_per_day:
            return _GateDecision(False, f"max_per_day reached ({sent_today}/{max_per_day})")

        if isinstance(last_sent_utc, str):
            try:
                last = datetime.fromisoformat(last_sent_utc.replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                mins = (ctx.now_utc - last).total_seconds() / 60.0
                if mins < float(min_interval_minutes):
                    return _GateDecision(False, f"min_interval ({int(mins)}m<{min_interval_minutes}m)")
            except Exception:
                logger.debug("Could not parse last_sent_utc for rate-limit check", exc_info=True)
                pass

        return _GateDecision(True, "rate_limit ok")

    def _random_gate(self, ctx: StepContext, stage_cfg: Dict[str, Any], boundary_date_local: str) -> _GateDecision:
        rnd = stage_cfg.get("randomization", {}) if isinstance(stage_cfg, dict) else {}
        if not isinstance(rnd, dict):
            return _GateDecision(True, "randomization disabled")
        prob = float(rnd.get("probability", 1.0))
        bucket_minutes = int(rnd.get("bucket_minutes", 15))
        if prob >= 1.0:
            return _GateDecision(True, "randomization off")
        if bucket_minutes <= 0:
            bucket_minutes = 15

        # Deterministic per time bucket to avoid weirdness across restarts.
        minute_bucket = (ctx.now_local.minute // bucket_minutes) * bucket_minutes
        bucket = ctx.now_local.replace(minute=minute_bucket, second=0, microsecond=0)
        seed = f"{boundary_date_local}:{self.step_id}:{bucket.isoformat()}"
        r = random.Random(seed).random()
        if r > prob:
            return _GateDecision(False, f"random_skip p={prob:.2f} r={r:.2f}")
        return _GateDecision(True, f"random_ok p={prob:.2f} r={r:.2f}")

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        stage_cfg = ctx.step_config or {}
        boundary_date_local = self._boundary_date_local(ctx)

        for gate in (
            self._quiet_hours_veto(ctx, stage_cfg),
            self._presence_veto(ctx, stage_cfg),
            self._calendar_veto(ctx, stage_cfg),
            self._rate_limit_veto(ctx, stage_cfg, boundary_date_local),
            self._random_gate(ctx, stage_cfg, boundary_date_local),
        ):
            if not gate.ok:
                return False, gate.reason
        return True, "ready"

    def _build_context(self, ctx: StepContext, boundary_date_local: str) -> Dict[str, Any]:
        # Recent starters
        pointer = self._load_latest_pointer(ctx)
        recent = pointer.get("recent_starters", [])
        if not isinstance(recent, list):
            recent = []
        recent = recent[-10:]

        # Weather summary
        weather = ctx.read_resource("resource_weather.json") or {}
        weather_summary = ""
        if isinstance(weather, dict):
            try:
                loc = (weather.get("location") or {}).get("city")
                cur = weather.get("current") or {}
                if loc and isinstance(cur, dict) and cur.get("temperature") is not None and cur.get("condition"):
                    weather_summary = f"{loc}: {cur.get('temperature')} F, {cur.get('condition')}"
            except Exception:
                logger.debug("Could not build weather_summary", exc_info=True)
                weather_summary = ""

        # Routine markdown (compact)
        routine_md = ""
        try:
            p = Path(ctx.resources_dir) / "resource_dayflow_routine.md"
            if p.exists():
                routine_md = p.read_text(encoding="utf-8")
        except Exception:
            logger.debug("Could not load routine_md", exc_info=True)
            routine_md = ""

        # Daily context + health
        daily_context = ctx.read_resource("resource_daily_context_generator_output.json") or {}
        health = ctx.read_resource("resource_health_inference_output.json") or {}

        # Calendar next 7 days (cheap source)
        try:
            from app.assistant.pipelines.dayflow.utils.context_sources import get_calendar_events_upcoming_for_daily_context

            calendar_next_7 = get_calendar_events_upcoming_for_daily_context(hours=24 * 7)
        except Exception:
            logger.debug("Could not load calendar_next_7", exc_info=True)
            calendar_next_7 = []

        # Recent chat excerpts (light)
        recent_chat_excerpts: List[Dict[str, Any]] = []
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.chat_formatting import messages_to_chat_excerpts

            cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
            scope = resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config)
            msgs = DI.global_blackboard.get_recent_chat_since_utc(
                cutoff,
                limit=None,
                room_id=scope["room_id"],
                room_surface=scope["room_surface"],
                shared_room_ids=scope.get("shared_chat_room_ids", []),
            )
            recent_chat_excerpts = messages_to_chat_excerpts(msgs, consumer_room_id=scope["room_id"])
        except Exception:
            logger.debug("Could not load recent_chat_excerpts", exc_info=True)
            recent_chat_excerpts = []

        # Timeline head (heavy-ish, but we cap)
        timeline_head: List[Dict[str, Any]] = []
        try:
            latest = ctx.read_resource("resource_daily_timeline_latest.json") or {}
            if isinstance(latest, dict) and isinstance(latest.get("archive_path"), str):
                tpath = Path(latest["archive_path"])
                tdata = _read_json_optional(tpath)
                if isinstance(tdata, dict) and isinstance(tdata.get("items"), list):
                    timeline_head = (tdata.get("items") or [])[:20]
                elif isinstance(tdata, list):
                    timeline_head = tdata[:20]
        except Exception:
            logger.debug("Could not load timeline_head", exc_info=True)
            timeline_head = []

        # Random KG node + neighborhood for creative inspiration
        kg_random_topic = self._get_random_kg_topic()

        # Life timeline gaps — surfaces estimated-date confirmations, multi-year
        # coverage holes, and prose-only facts the user could pin. Lets the
        # starter weave grounding questions naturally instead of always
        # picking from random recent activity.
        life_gaps = self._get_life_gaps()

        # Very lightweight hints
        day_type_hints = "workday" if ctx.now_local.weekday() < 5 else "weekend"

        return {
            "boundary_date_local": boundary_date_local,
            "day_of_week": ctx.now_local.strftime("%A"),
            "history_scope": resolve_room_scope(pipeline_config=ctx.pipeline_config, step_config=ctx.step_config),
            "conversation_starters_recent": recent,
            "day_type_hints": day_type_hints,
            "weather_summary": weather_summary,
            "routine_md": routine_md,
            "daily_context": daily_context if isinstance(daily_context, dict) else {},
            "health": health if isinstance(health, dict) else {},
            "calendar_next_7_days": calendar_next_7,
            "recent_chat_excerpts": recent_chat_excerpts,
            "timeline_head": timeline_head,
            "kg_random_topic": kg_random_topic,
            "life_gaps": life_gaps,
        }

    def _get_life_gaps(self) -> Dict[str, Any]:
        """Compute conversation hooks from the primary user's life timeline:
        estimated dates worth confirming, multi-year coverage holes, and
        prose-only high-importance facts that could be pinned to an ISO date.

        Pure SQL + sort. No LLM cost. Returned to the conversation_starter
        agent as the ``life_gaps`` context item.

        Fail-soft: any error returns an empty dict so the starter degrades to
        its other context sources rather than failing.
        """
        try:
            from app.assistant.kg.timeline_gaps import compute_timeline_gaps
            from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
            from app.assistant.kg_core.user_identity import get_primary_user_name
            from app.models.db_manager import get_db_manager

            user_name = get_primary_user_name()
            with get_db_manager().read_session() as session:
                user_node = (
                    session.query(Node)
                    .filter(Node.label == user_name, Node.node_type == "Entity")
                    .first()
                )
                primary_id = user_node.id if user_node else None
            if not primary_id:
                return {}
            return compute_timeline_gaps(primary_id)
        except Exception:
            logger.debug("Could not compute life_gaps", exc_info=True)
            return {}

    def _get_random_kg_topic(self) -> Dict[str, Any]:
        """Pick a random KG node and return it with its immediate neighborhood."""
        try:
            from sqlalchemy import func as sa_func
            from app.models.db_manager import get_db_manager
            from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge

            with get_db_manager().read_session() as session:
                # Pick a random node that has connections and reasonable content.
                # Prefer nodes with higher importance/pagerank so we get interesting topics.
                node = (
                    session.query(Node)
                    .filter(Node.description.isnot(None))
                    .filter(Node.node_type.in_(["Entity", "Concept", "Goal", "Event"]))
                    .order_by(sa_func.random())
                    .limit(1)
                    .first()
                )
                if node is None:
                    return {}

                # Get immediate connections. LEFT-style outerjoin so edges
                # to pod URIs (no kg_node row by design) still surface; pod
                # labels hydrate from pod_store via _pod_label() below.
                from app.assistant.pod_store.pod_uri import POD_URI_RE
                _pod_cache: dict = {}

                def _pod_label(uri: str):
                    if uri in _pod_cache:
                        return _pod_cache[uri]
                    if not uri or not POD_URI_RE.fullmatch(uri):
                        _pod_cache[uri] = (None, None)
                        return _pod_cache[uri]
                    from app.assistant.pod_store.pod_store import PodStore
                    pod = PodStore().get(uri)
                    if pod is None:
                        _pod_cache[uri] = (None, None)
                    else:
                        _pod_cache[uri] = ((pod.one_liner or uri)[:200], "Pod")
                    return _pod_cache[uri]

                connections = []
                outgoing = (
                    session.query(Edge, Node)
                    .outerjoin(Node, Edge.target_id == Node.id)
                    .filter(Edge.source_id == node.id)
                    .limit(8)
                    .all()
                )
                for edge, target in outgoing:
                    if target is not None:
                        t_label, t_type = target.label, target.node_type
                    else:
                        t_label, t_type = _pod_label(edge.target_id)
                    if t_label is None:
                        continue
                    connections.append({
                        "relationship": edge.relationship_type,
                        "target": t_label,
                        "target_type": t_type,
                    })

                incoming = (
                    session.query(Edge, Node)
                    .outerjoin(Node, Edge.source_id == Node.id)
                    .filter(Edge.target_id == node.id)
                    .limit(8)
                    .all()
                )
                for edge, source in incoming:
                    if source is not None:
                        s_label, s_type = source.label, source.node_type
                    else:
                        s_label, s_type = _pod_label(edge.source_id)
                    if s_label is None:
                        continue
                    connections.append({
                        "relationship": edge.relationship_type,
                        "source": s_label,
                        "source_type": s_type,
                    })

                return {
                    "label": node.label,
                    "type": node.node_type,
                    "category": node.category or "",
                    "description": (node.description or "")[:300],
                    "connections": connections,
                }
        except Exception:
            logger.debug("Could not load random KG topic", exc_info=True)
            return {}

    def _emit_to_user(self, text: str) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.assistant_name import get_assistant_name
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData

            metadata = {"reply_to": {"type": "socketio", "room_id": "master_room"}}

            msg = UserMessage(
                data_type="user_msg",
                sender=get_assistant_name(),
                receiver=None,
                content=text,
                timestamp=datetime.now(timezone.utc),
                role="assistant",
                metadata=metadata,
                user_message_data=UserMessageData(feed=None, chat=text, tts=False, tts_text=None),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
        except Exception as e:
            logger.warning("ConversationStarterStage: failed to emit to user: %s", e)

    def run(self, ctx: StepContext) -> StepResult:
        boundary_date_local = self._boundary_date_local(ctx)
        stage_cfg = ctx.step_config or {}

        # ── User-controlled frequency gate ────────────────────────────────────
        # Read assistant_core.json conversation_starter.frequency (set in the
        # /settings/assistant UI). Maps to a min-interval that overrides the
        # pipeline's default schedule. "off" disables entirely.
        freq = _load_conversation_starter_frequency()
        if freq == "off":
            return StepResult(output={"status": "skipped", "reason": "frequency=off"})
        min_interval_seconds = _MIN_INTERVAL_FOR_FREQUENCY.get(freq, 600)
        pointer_for_gate = self._load_latest_pointer(ctx)
        last_sent_iso = pointer_for_gate.get("last_sent_utc")
        if last_sent_iso:
            try:
                last_sent_dt = datetime.fromisoformat(last_sent_iso.replace("Z", "+00:00"))
                elapsed = (ctx.now_utc - last_sent_dt).total_seconds()
                if elapsed < min_interval_seconds:
                    return StepResult(output={
                        "status": "skipped",
                        "reason": f"frequency={freq} (need {int(min_interval_seconds)}s, elapsed {int(elapsed)}s)",
                    })
            except Exception:
                logger.debug("[ConversationStarter] frequency gate: bad last_sent_utc; allowing", exc_info=True)

        # ── Context activation memo fast-path ─────────────────────────────────
        # If the context_engine has prepared an unaddressed memo (background
        # pipeline ran after the user's last message), surface the first
        # suggested question from it instead of running the full LLM agent.
        try:
            from app.assistant.context_engine.context_memo import get_active_memo, mark_memo_addressed
            memo_msg = get_active_memo()
            if memo_msg is not None:
                memo_text = (getattr(memo_msg, "content", None) or "").strip()
                first_question = _extract_first_question(memo_text)
                if first_question:
                    mark_memo_addressed(memo_msg)
                    sid = _starter_id(first_question)
                    pointer = self._load_latest_pointer(ctx)
                    recent = pointer.get("recent_starters", [])
                    recent_ids = {str(x.get("starter_id")) for x in recent} if isinstance(recent, list) else set()
                    if sid not in recent_ids:
                        self._emit_to_user(first_question)
                        try:
                            from app.assistant.ServiceLocator.service_locator import DI
                            from app.assistant.utils.assistant_name import get_assistant_name
                            from app.assistant.utils.pydantic_classes import Message as _Msg
                            _assistant_name = get_assistant_name()
                            DI.global_blackboard.add_msg(
                                _Msg(
                                    data_type="user_msg",
                                    sender=_assistant_name,
                                    content=first_question,
                                    is_chat=True,
                                    role="assistant",
                                    sub_data_type=["proactive", "context_activation"],
                                    room_id="master_room",
                                    room_surface="ui",
                                    room_context_id="main",
                                    room_visibility="owner_only",
                                    room_message_direction="outbound",
                                    room_speaker_id="system:proactive",
                                    room_speaker_name=_assistant_name,
                                    room_speaker_role="assistant",
                                    metadata={
                                        "source": "context_engine",
                                        "starter_id": sid,
                                        "boundary_date_local": boundary_date_local,
                                    },
                                )
                            )
                            from app.assistant.maintenance_manager.save_to_unified_db import save_proactive_chat_message
                            save_proactive_chat_message(
                                content=first_question,
                                sender=_assistant_name,
                                sub_data_type=["proactive", "context_activation"],
                            )
                        except Exception as e:
                            logger.warning("ConversationStarterStage: failed to record context memo question: %s", e)
                        logger.info(
                            "ConversationStarterStage: surfaced context_activation question brief_id=%s",
                            (getattr(memo_msg, "metadata", None) or {}).get("brief_id", "?"),
                        )
                        return StepResult(output={"status": "ok", "source": "context_activation_memo", "starter_text": first_question})
        except Exception as e:
            logger.error("ConversationStarterStage: context_activation memo check failed: %s", e)
            logger.debug("ConversationStarterStage: context_activation memo exception details", exc_info=True)
            raise
        # ── End memo fast-path ─────────────────────────────────────────────────

        context = self._build_context(ctx, boundary_date_local)
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("conversation_starter")
            scope_context = self._build_pipeline_scope_context(context)
            result = agent.action_handler(Message(agent_input=context, scope_context=scope_context))
            data = getattr(result, "data", None)
        except Exception as e:
            return StepResult(output={"status": "error", "error": str(e)})

        if not isinstance(data, dict):
            return StepResult(output={"status": "error", "error": "agent returned invalid data"})

        should = bool(data.get("should_initiate", False))
        reason = str(data.get("reason") or "").strip()
        starter_text = data.get("starter_text")

        if not should:
            return StepResult(output={"status": "skipped", "reason": reason})

        if not isinstance(starter_text, str) or not starter_text.strip():
            return StepResult(output={"status": "error", "error": "missing starter_text"})

        text = starter_text.strip()
        sid = _starter_id(text)

        # Last-minute dedupe against recent ids
        pointer = self._load_latest_pointer(ctx)
        recent = pointer.get("recent_starters", [])
        recent_ids = {str(x.get("starter_id")) for x in recent} if isinstance(recent, list) else set()
        if sid in recent_ids:
            return StepResult(output={"status": "skipped", "reason": "duplicate_starter_id"})

        # Emit message to user
        self._emit_to_user(text)

        # Record as a genuine assistant chat message in the global blackboard.
        # This makes it show up in "recent chat" excerpts and downstream context like normal Emi chat.
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.assistant_name import get_assistant_name
            from app.assistant.utils.pydantic_classes import Message

            _assistant_name = get_assistant_name()
            DI.global_blackboard.add_msg(
                Message(
                    data_type="user_msg",
                    sender=_assistant_name,
                    content=text,
                    is_chat=True,
                    role="assistant",
                    sub_data_type=["proactive", "conversation_starter"],
                    room_id="master_room",
                    room_surface="ui",
                    room_context_id="main",
                    room_visibility="owner_only",
                    room_message_direction="outbound",
                    room_speaker_id="system:proactive",
                    room_speaker_name=_assistant_name,
                    room_speaker_role="assistant",
                    metadata={
                        "source": "conversation_starter",
                        "starter_id": sid,
                        "boundary_date_local": boundary_date_local,
                    },
                )
            )
            from app.assistant.maintenance_manager.save_to_unified_db import save_proactive_chat_message
            save_proactive_chat_message(
                content=text,
                sender=_assistant_name,
                sub_data_type=["proactive", "conversation_starter"],
            )
        except Exception as e:
            logger.warning("ConversationStarterStage: failed to record to global blackboard: %s", e)

        # Append JSONL log
        day_dir = self._day_dir(ctx, boundary_date_local)
        log_path = day_dir / "conversation_starters.jsonl"
        entry = {
            "schema_version": 1,
            "starter_id": sid,
            "boundary_date_local": boundary_date_local,
            "timestamp_utc": ctx.now_utc.isoformat(),
            "timestamp_local": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
            "intent": data.get("intent"),
            "topics": data.get("topics") if isinstance(data.get("topics"), list) else [],
            "starter_text": text,
            "decision_reason": reason,
            "feedback": None,
        }
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("ConversationStarterStage: failed to append log: %s", e)

        # Update pointer resource (small, injected)
        last_boundary = pointer.get("boundary_date_local")
        sent_today = int(pointer.get("sent_today", 0) or 0) if last_boundary == boundary_date_local else 0
        sent_today += 1
        recent_list = pointer.get("recent_starters", [])
        if not isinstance(recent_list, list):
            recent_list = []
        recent_list.append(
            {
                "starter_id": sid,
                "timestamp_utc": ctx.now_utc.isoformat(),
                "intent": entry["intent"],
                "topics": entry["topics"],
                "text": text,
            }
        )
        recent_list = recent_list[-30:]

        self._save_latest_pointer(
            ctx,
            {
                "schema_version": 1,
                "boundary_date_local": boundary_date_local,
                "last_sent_utc": ctx.now_utc.isoformat(),
                "last_sent_local": ctx.now_local.strftime("%Y-%m-%d %H:%M"),
                "last_starter_id": sid,
                "sent_today": sent_today,
                "recent_starters": recent_list,
                "archive_log_path": str(log_path),
            },
        )

        return StepResult(output={"status": "sent", "starter_id": sid}, debug={"reason": reason})

    def reset(self, ctx: StepContext) -> None:
        # No-op: daily counter handled in pointer by boundary_date_local.
        return None

