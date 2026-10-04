"""The concern brief: what everyone working on a concern reads first (2026-09-30, step 5).

A concern's record grows as a list of evidence refs, journal lines and work outcomes. Dayflow saw
none of it: the planner got the title and notes cut at 200 characters, and the agents doing the work
saw nothing of the concern. The brief is the record made readable for someone new to the matter:
what it is, why it matters, what is known (each fact with its source), what was tried and how it
went, what the owner said, what depends on it, open questions, and a recommendation.

`subconscious::brief` writes it for every open concern whose record changed since its brief was
written, run by the brain's wake (subconscious/brain_wake.py) after the brain step. The brief is stored on the concern
(`brief`, with the `basis` fingerprint of the record it was written from, and of WRITER_VERSION);
a concern whose brief could not be written carries `brief_error` with the same fingerprint and is
not retried until its record, or the writer, changes. Every cited source must contain a ref the
writer was shown (evidence, work ids, calendar anchors, reminder refs), or name the concern's
journal or notes, or the knowledge graph; one correction. Sources are shown and cited by label (S1, S2,
...), and stored as the refs they stand for (2026-10-02).

Dayflow reads it through `briefs_for_refs`: every agent working on a work object that cites a
concern (architect, worker, finalizer) gets that concern's brief whole, and the work attached to
it so far (`attempts`) (dayflow_orchestrator/work_context.work_data). The steward gets the same view
in a handoff (concern_handoff._brief_text), so it can judge whether another attempt is productive.

The brief decides nothing: the concern planner (subconscious/concern_planner.py) reads it and decides what
the concern needs now. (Until 2026-10-02 the brief's readiness decided that; until 2026-10-03 a standing
concern, made from the owner's beliefs, ended its brief in a readiness rule for its occurrences. Beliefs
are no longer made into concerns: docs/design/what_was_done_reaches_the_brain_2026-10-03.md.)
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
WRITER_VERSION = 3   # 3: readiness (act_now / hold / no_action), since removed
# A failed brief is tried again when the writer's revision changes, without rewriting the briefs that
# succeeded (raising WRITER_VERSION rewrites them all, and every new brief is planned again).
# 1: sources are shown and cited by label (S1, S2, ...), mapped back to their refs by code.
WRITER_REVISION = 1
_OWN_KEYS = ("brief", "brief_error", "plan", "plan_error")   # what is written from the record
_FIELDS = ("what", "why_it_matters", "tried", "owner_wishes", "recommendation")
# A fact from the concern's own journal or notes, or from a knowledge-graph description, has no ref.
_NAMED_SOURCES = ("journal", "notes", "knowledge graph")


def basis(concern: Dict[str, Any], bucket: str) -> str:
    """Fingerprint of the concern's record, excluding what is written from it (the brief, the plan)."""
    record = {k: v for k, v in concern.items() if k not in _OWN_KEYS}
    return hashlib.sha256(json.dumps([WRITER_VERSION, bucket, record], sort_keys=True, ensure_ascii=False,
                                     default=str).encode("utf-8")).hexdigest()


def attempts(concern: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The work attached to the concern, oldest first, as every reader shows it: objective, status,
    where it stands now (read live from the work store: what is done, what is still to come and what
    each step waits for), each judgment with the owner's replies as lines, and the ending."""
    from app.assistant.subconscious.concern_feedback import _reply_line
    from app.assistant.subconscious.work_links import progress
    works = sorted((concern.get("attached_work") or {}).values(), key=lambda w: str(w.get("attached_at") or ""))
    return [{"work_id": w["work_id"], "title": w.get("title"), "objective": w.get("objective"),
             "attached_at": w.get("attached_at"), "status": w.get("status"), "progress": progress(w["work_id"]),
             "judgments": [{**j, "replies": [_reply_line(r) for r in j.get("replies") or []]}
                           for j in w.get("judgments") or []],
             "ended": w.get("ended")}
            for w in works]


def _failed_on_this_record(concern: Dict[str, Any], b: str) -> bool:
    """The brief already failed on this record, with this revision of the writer."""
    error = concern.get("brief_error") or {}
    return error.get("basis") == b and error.get("writer") == WRITER_REVISION


def stale(register: Dict[str, Any], now_utc: Optional[datetime] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """Open concerns whose brief is missing or was written from an older record, minus those whose brief
    already failed on the current record. (bucket, concern). `now_utc` is kept for the callers' symmetry
    with the planner's `stale` and is not read."""
    out = []
    for bucket in ("active", "addressing"):
        for c in register.get(bucket) or []:
            b = basis(c, bucket)
            if _failed_on_this_record(c, b) or (c.get("brief") or {}).get("basis") == b:
                continue
            out.append((bucket, c))
    return out


def stale_reason(concern: Dict[str, Any], bucket: str) -> str:
    """Why a stale brief is written again, for the run record."""
    return "its record changed" if concern.get("brief") else "no brief yet"


def build_payload(concern: Dict[str, Any], bucket: str, *, calendar: str, now_utc: datetime) -> Dict[str, Any]:
    from app.assistant.subconscious import brain_step, kg_links, work_links
    from app.assistant.utils.time_utils import utc_to_local
    linked = work_links.linked_work([concern["concern_id"]], [])
    similar = work_links.similar_work([concern.get("title") or "", concern.get("notes") or ""],
                                      exclude=[w["work_id"] for w in linked])
    view = brain_step._concern_view("C1", {**concern, "_status": bucket})
    entities = kg_links.find_entities([view["title"] or "", view["notes"]] + [e["snippet"] for e in view["evidence"]])
    payload = {
        "now": utc_to_local(now_utc.isoformat()).strftime("%a %Y-%m-%d %H:%M"),
        "concern": view,
        "entities": entities, "shared": kg_links.shared([x["node_id"] for x in entities]),
        "facts": kg_links.facts_by_meaning([view["title"] or "", view["notes"]] + [e["snippet"] for e in view["evidence"]]),
        "linked_work": linked, "similar_work": similar,
        "active_work": work_links.active_work(), "reminders": work_links.live_reminders(now_utc),
        "calendar": calendar,
    }
    return label_sources(payload)


def label_sources(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Give every source shown a short label (S1, S2, ...), shown in its ref's place, so the writer cites a
    label instead of copying a long id (it once dropped one character of a pod id, and the brief failed on
    that record for good). `source_labels`: ref -> label, read by the shared templates; the calendar's
    refs are replaced in its text."""
    refs = allowed_refs(payload) + [w["work_id"] for w in payload["concern"]["attached_work"]]
    labels: Dict[str, str] = {}
    for ref in refs:
        labels.setdefault(ref, f"S{len(labels) + 1}")
    calendar = payload.get("calendar") or ""
    for ref, label in labels.items():
        if ref.startswith("calendar:"):
            calendar = calendar.replace(f"[{ref}]", f"[{label}]")
    return {**payload, "calendar": calendar, "source_labels": labels}


def allowed_refs(payload: Dict[str, Any]) -> List[str]:
    """Every ref shown: the concern's evidence, work ids, calendar anchors, reminders (read before the
    calendar's refs are replaced by labels)."""
    refs = [str(e.get("ref") or "") for e in payload["concern"]["evidence"]]
    refs += [w["work_id"] for key in ("linked_work", "similar_work", "active_work") for w in payload[key]]
    refs += re.findall(r"calendar:[^\s\]\)]+", payload.get("calendar") or "")
    refs += [r["ref"] for r in payload["reminders"]]
    return [r for r in refs if r]


def hold_until_utc(value: str, now_utc: datetime) -> datetime:
    """A local 'YYYY-MM-DD HH:MM' as UTC; raises when unreadable or not in the future."""
    from app.assistant.utils.time_utils import get_local_timezone
    local = datetime.strptime(str(value or "").strip(), "%Y-%m-%d %H:%M").replace(tzinfo=get_local_timezone())
    when = local.astimezone(timezone.utc)
    if when <= now_utc:
        raise ValueError(f"hold_until {value!r} is not in the future")
    return when


def _source_parts(source: Any) -> List[str]:
    return [p.strip() for p in str(source or "").split(";") if p.strip()]


def _problems(data: Any, labels: Dict[str, str]) -> List[str]:
    """What is wrong with a brief: an empty field, or a fact citing a source that was not shown."""
    if not isinstance(data, dict):
        return ["no brief"]
    out = [f"{f} is empty" for f in _FIELDS if not str(data.get(f) or "").strip()]
    shown = set(labels.values())
    for fact in data.get("known") or []:
        source = str(fact.get("source") or "").strip()
        parts = _source_parts(source)
        bad = [p for p in parts if p.lower() not in _NAMED_SOURCES and p not in shown]
        if not parts or bad:
            out.append(f"source {source!r} (for {str(fact.get('fact'))!r}) is not a label shown: cite the "
                       "label of what was shown (S1, S2, ...), or journal, notes or knowledge graph")
    return out


def _with_refs(known: List[Dict[str, Any]], labels: Dict[str, str]) -> List[Dict[str, Any]]:
    """Each fact's cited labels as the refs they stand for: every reader of the brief sees refs."""
    ref_of = {label: ref for ref, label in labels.items()}
    return [{**f, "source": "; ".join(ref_of.get(p, p) for p in _source_parts(f.get("source")))} for f in known]


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="brief", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::brief", actor_id="subconscious::brief")


def write_brief(concern: Dict[str, Any], bucket: str, *, calendar: str,
                call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, Any]:
    """The brief for one concern; raises when still invalid after one correction."""
    now = datetime.now(timezone.utc)
    payload = build_payload(concern, bucket, calendar=calendar, now_utc=now)
    labels = payload["source_labels"]
    call = call or _agent_call
    data = call(payload)
    problems = _problems(data, labels)
    if problems:
        from app.assistant.subconscious.brain_trace import step
        logger.warning("[brief] invalid brief for %s, one correction: %s", concern["concern_id"], problems)
        step("brief", "answer refused, one correction", subject=concern["concern_id"], problems=problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, labels)
        if problems:
            raise ValueError(f"brief still invalid after one correction: {problems}")
    brief = {k: data.get(k) for k in ("what", "why_it_matters", "known", "tried", "owner_wishes",
                                       "depends_on_it", "open_questions", "recommendation")}
    brief["known"] = _with_refs(brief["known"] or [], labels)
    return brief


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
        cid = concern["concern_id"]
        from app.assistant.subconscious.brain_trace import step, trace
        step("brief", "stale", subject=cid, title=concern.get("title"), reason=stale_reason(concern, bucket))
        try:
            with trace(stage="brief", concern_id=cid):
                brief = write_brief(concern, bucket, calendar=calendar, call=call)
        except Exception as exc:
            logger.error("[brief] could not write the brief for %s; not retried until it changes: %s",
                         cid, exc, exc_info=True)
            step("brief", "failed: not retried until the record changes", subject=cid,
                 error=f"{type(exc).__name__}: {exc}")
            persist.set_concern_brief(cid, written_from, error=f"{type(exc).__name__}: {exc}",
                                      connect=register_connect)
            failed += 1
            continue
        if persist.set_concern_brief(cid, written_from, brief=brief, connect=register_connect):
            step("brief", "written", subject=cid)
            written += 1
        else:
            step("brief", "not stored: the record changed while it was written", subject=cid)
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
                    "brief": c.get("brief"), "plan": c.get("plan"), "attempts": attempts(c),
                    "brief_current": (c.get("brief") or {}).get("basis") == basis(c, bucket)})
    return out
