"""The concern brief: what everyone working on a concern reads first (2026-09-30, step 5).

A concern's record grows as a list of evidence refs, journal lines and work outcomes. Dayflow saw
none of it: the planner got the title and notes cut at 200 characters, and the agents doing the work
saw nothing of the concern. The brief is the record made readable for someone new to the matter:
what it is, why it matters, what is known (each fact with its source), what was tried and how it
went, what the owner said, what depends on it, open questions, and a recommendation.

`subconscious::brief` writes it for every open concern whose record changed since its brief was
written, run by the brain_gate routine after the brain step. The brief is stored on the concern
(`brief`, with the `basis` fingerprint of the record it was written from, and of WRITER_VERSION);
a concern whose brief could not be written carries `brief_error` with the same fingerprint and is
not retried until its record, or the writer, changes. Every cited source must contain a ref the
writer was shown (evidence, work ids, calendar anchors, reminder refs), or name the concern's
journal or notes, or the knowledge graph; one correction.

Dayflow reads it through `briefs_for_refs`: every agent working on a work object that cites a
concern (architect, worker, finalizer) gets that concern's brief whole (dayflow_orchestrator/
work_context.work_data).

The brief ends in a readiness decision: act_now (with a broad task; subconscious/concern_handoff.py
hands it to the planner), hold (with a local date and time, stored as `hold_until_utc`; when it
passes the concern is briefed again, so the decision is remade on what is known then), or
no_action.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::brief"
# Part of every brief's basis: raising it rewrites every brief, and retries every brief the older
# writer failed on. 2: sources are checked against the refs shown, reminders carry refs, KG entities.
WRITER_VERSION = 3   # 3: readiness (act_now / hold / no_action)
_OWN_KEYS = ("brief", "brief_error")
_FIELDS = ("what", "why_it_matters", "tried", "owner_wishes", "recommendation")
# A fact from the concern's own journal or notes, or from a knowledge-graph description, has no ref.
_NAMED_SOURCES = ("journal", "notes", "knowledge graph")


def basis(concern: Dict[str, Any], bucket: str) -> str:
    """Fingerprint of the concern's record, excluding the brief itself."""
    record = {k: v for k, v in concern.items() if k not in _OWN_KEYS}
    return hashlib.sha256(json.dumps([WRITER_VERSION, bucket, record], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def _held_until_passed(brief: Dict[str, Any], now: datetime) -> bool:
    readiness = brief.get("readiness") or {}
    when = readiness.get("hold_until_utc")
    return readiness.get("decision") == "hold" and bool(when) and datetime.fromisoformat(when) <= now


def stale(register: Dict[str, Any], now_utc: Optional[datetime] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """Open concerns whose brief is missing, was written from an older record, or holds until a time
    that has passed; minus those whose brief already failed on the current record. (bucket, concern)."""
    now = now_utc or datetime.now(timezone.utc)
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            b = basis(c, bucket)
            brief = c.get("brief") or {}
            if (c.get("brief_error") or {}).get("basis") == b:
                continue
            if brief.get("basis") == b and not _held_until_passed(brief, now):
                continue
            out.append((bucket, c))
    return out


def build_payload(concern: Dict[str, Any], bucket: str, *, calendar: str, now_utc: datetime) -> Dict[str, Any]:
    from app.assistant.subconscious import brain_step, kg_links, work_links
    from app.assistant.utils.time_utils import utc_to_local
    linked = work_links.linked_work([concern["concern_id"]], [])
    similar = work_links.similar_work([concern.get("title") or "", concern.get("notes") or ""],
                                      exclude=[w["work_id"] for w in linked])
    view = brain_step._concern_view("C1", {**concern, "_status": bucket})
    entities = kg_links.find_entities([view["title"] or "", view["notes"]] + [e["snippet"] for e in view["evidence"]])
    return {
        "now": utc_to_local(now_utc.isoformat()).strftime("%a %Y-%m-%d %H:%M"),
        "concern": view,
        "entities": entities, "shared": kg_links.shared([x["node_id"] for x in entities]),
        "linked_work": linked, "similar_work": similar,
        "active_work": work_links.active_work(), "reminders": work_links.live_reminders(now_utc),
        "calendar": calendar,
    }


def allowed_refs(payload: Dict[str, Any]) -> List[str]:
    """Every ref the writer is shown: the concern's evidence, work ids, calendar anchors, reminders."""
    refs = [str(e.get("ref") or "") for e in payload["concern"]["evidence"]]
    refs += [w["work_id"] for key in ("linked_work", "similar_work", "active_work") for w in payload[key]]
    refs += re.findall(r"calendar:[^\s\]\)]+", payload.get("calendar") or "")
    refs += [r["ref"] for r in payload["reminders"]]
    return [r for r in refs if r]


def _cites_shown(part: str, refs: List[str]) -> bool:
    """A cited source part is good when it contains a shown ref, or a shown ref's id without its
    `kind:` prefix (evidence is displayed as "kind ref", and older evidence refs are bare ids)."""
    for ref in refs:
        bare = ref.split(":", 1)[-1]
        if ref in part or (len(bare) >= 8 and bare in part):
            return True
    return False


def hold_until_utc(value: str, now_utc: datetime) -> datetime:
    """A local 'YYYY-MM-DD HH:MM' as UTC; raises when unreadable or not in the future."""
    from app.assistant.utils.time_utils import get_local_timezone
    local = datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=get_local_timezone())
    when = local.astimezone(timezone.utc)
    if when <= now_utc:
        raise ValueError(f"hold_until {value!r} is not in the future")
    return when


def _readiness_problems(readiness: Any, now_utc: datetime) -> List[str]:
    if not isinstance(readiness, dict):
        return ["readiness is missing"]
    decision = readiness.get("decision")
    if decision not in ("act_now", "hold", "no_action"):
        return [f"readiness decision {decision!r} is not act_now, hold or no_action"]
    out = [] if str(readiness.get("why") or "").strip() else ["readiness why is empty"]
    if decision == "act_now" and not str(readiness.get("task") or "").strip():
        out.append("act_now needs a task for the planner")
    if decision == "hold":
        try:
            hold_until_utc(readiness.get("hold_until"), now_utc)
        except ValueError as exc:
            out.append(f"hold needs hold_until as a future local YYYY-MM-DD HH:MM: {exc}")
    return out


def _problems(data: Any, refs: List[str], now_utc: Optional[datetime] = None) -> List[str]:
    if not isinstance(data, dict):
        return ["no brief"]
    out = [f"{f} is empty" for f in _FIELDS if not str(data.get(f) or "").strip()]
    out += _readiness_problems(data.get("readiness"), now_utc or datetime.now(timezone.utc))
    for fact in data.get("known") or []:
        source = str(fact.get("source") or "").strip()
        parts = [p.strip() for p in re.split(r";", source) if p.strip()]
        bad = [p for p in parts if p.lower() not in _NAMED_SOURCES and not _cites_shown(p, refs)]
        if not parts or bad:
            out.append(f"source {source!r} (for {str(fact.get('fact'))!r}) names nothing that was shown: "
                       "cite the ref as shown, or journal, notes or knowledge graph")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="brief", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::brief", actor_id="subconscious::brief")


def write_brief(concern: Dict[str, Any], bucket: str, *, calendar: str,
                call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
    """The brief for one concern; raises when still invalid after one correction."""
    now = datetime.now(timezone.utc)
    payload = build_payload(concern, bucket, calendar=calendar, now_utc=now)
    refs = allowed_refs(payload)
    call = call or _agent_call
    data = call(payload)
    problems = _problems(data, refs, now)
    if problems:
        logger.warning("[brief] invalid brief for %s, one correction: %s", concern["concern_id"], problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, refs, now)
        if problems:
            raise ValueError(f"brief still invalid after one correction: {problems}")
    readiness = {k: data["readiness"].get(k) for k in ("decision", "hold_until", "task", "why")}
    if readiness["decision"] == "hold":
        readiness["hold_until_utc"] = hold_until_utc(readiness["hold_until"], now).isoformat()
    return {**{k: data.get(k) for k in ("what", "why_it_matters", "known", "tried", "owner_wishes",
                                        "depends_on_it", "open_questions", "recommendation")},
            "readiness": readiness}


def run_briefs(*, call=None, register_connect=None, calendar: Optional[str] = None) -> Dict[str, Any]:
    """Write the brief of every open concern whose record changed. Free when none did."""
    from app.assistant.subconscious import persist
    from app.assistant.subconscious.concern_store import load_register
    todo = stale(load_register(connect=register_connect))
    if not todo:
        return {"briefs_written": 0, "briefs_failed": 0}
    if calendar is None:
        from app.assistant.subconscious.brain_step import _calendar
        calendar = _calendar(datetime.now(timezone.utc))
    written = failed = 0
    for bucket, concern in todo:
        written_from = basis(concern, bucket)
        from app.assistant.subconscious.brain_trace import trace
        try:
            with trace(stage="brief", concern_id=concern["concern_id"]):
                brief = write_brief(concern, bucket, calendar=calendar, call=call)
        except Exception as exc:
            logger.error("[brief] could not write the brief for %s; not retried until it changes: %s",
                         concern["concern_id"], exc, exc_info=True)
            persist.set_concern_brief(concern["concern_id"], written_from, error=f"{type(exc).__name__}: {exc}",
                                      connect=register_connect)
            failed += 1
            continue
        if persist.set_concern_brief(concern["concern_id"], written_from, brief=brief, connect=register_connect):
            written += 1
    logger.info("[brief] written=%d failed=%d", written, failed)
    return {"briefs_written": written, "briefs_failed": failed}


def briefs_for_refs(refs: List[str]) -> List[Dict[str, Any]]:
    """The concerns a work object cites (full ids or `concern:<8+ chars>`), with their briefs, for the
    agents working on it. This runs on every render of the work object, so a ref that matches no
    concern, or more than one, is logged as an error and shown to the agent as unresolved rather
    than stopping the work from rendering."""
    from app.assistant.subconscious.concern_store import load_register
    wanted = [str(r).strip() for r in refs if str(r).strip()]
    if not wanted:
        return []
    register = load_register()
    every = [(b, c) for b in ("active", "addressing", "resolved", "dormant") for c in register.get(b) or []]
    out = []
    for ref in wanted:
        key = ref[len("concern:"):] if ref.startswith("concern:") else ref
        matches = [(b, c) for b, c in every
                   if c.get("concern_id") == key or (len(key) >= 8 and str(c.get("concern_id")).startswith(key))]
        if len(matches) != 1:
            logger.error("[brief] work cites concern ref %r, which matches %d concerns", ref, len(matches))
            out.append({"ref": ref, "unresolved": f"matches {len(matches)} concerns in the register"})
            continue
        bucket, c = matches[0]
        out.append({"concern_id": c["concern_id"], "title": c.get("title"), "status": bucket,
                    "done_when": c.get("done_when"), "owner_words": (c.get("owner_request") or {}).get("words"),
                    "brief": c.get("brief"),
                    "brief_current": (c.get("brief") or {}).get("basis") == basis(c, bucket)})
    return out
