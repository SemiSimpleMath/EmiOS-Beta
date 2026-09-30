"""The gate: routes every brain-inbox event against the open concerns.

One `subconscious::gate` call per page of pending events. Each event gets exactly one route —
`concern` (with the concerns it bears on), `new_matter` or `none` — validated here: every event
answered once, only the labels given, a concern route names at least one concern. One correction
round; an answer still invalid after it marks the page `failed`, which the noticer still reads
(a routing failure must not drop an event).

Labels are request-local (E1… for events, C1… for concerns) and mapped back by code, so the model
never copies a concern_id or a message id.

When anything is passed on (concern or new_matter), a noticer tick is triggered — cooldown-guarded,
shared with answer capture — so a report reaches the brain within minutes, not at tomorrow's 04:00.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::gate"
_PAGE_CHARS = 24000   # events per call are bounded by size, never cut


def open_concerns(register: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Active + addressing concerns, each tagged with its bucket."""
    return [{**c, "_bucket": b} for b in ("active", "addressing") for c in register.get(b) or []]


def _event_view(label: str, e: Dict[str, Any]) -> Dict[str, Any]:
    from app.assistant.utils.time_utils import utc_to_local
    return {"label": label, "time": utc_to_local(e["occurred_at"]).strftime("%a %Y-%m-%d %H:%M"),
            "room": e.get("room_id") or "?", "speaker": e.get("speaker") or "user",
            "text": e["text"], "replying_to": e.get("replying_to")}


def _problems(data: Any, event_labels: List[str], concern_labels: List[str]) -> List[str]:
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["no decisions list"]
    out, seen = [], []
    for d in data["decisions"]:
        ev = str((d or {}).get("event") or "").strip()
        if ev not in event_labels:
            out.append(f"event {ev!r} is not one of the events given")
            continue
        seen.append(ev)
        route, named = d.get("route"), d.get("concerns") or []
        if route not in ("concern", "new_matter", "none"):
            out.append(f"{ev}: route {route!r} is not concern, new_matter or none")
        bad = [x for x in named if x not in concern_labels]
        if bad:
            out.append(f"{ev}: {bad} are not concern labels given")
        if route == "concern" and not named:
            out.append(f"{ev}: route concern names no concern")
    dup = sorted({e for e in seen if seen.count(e) > 1})
    if dup:
        out.append(f"answered more than once: {dup}")
    missing = [e for e in event_labels if e not in seen]
    if missing:
        out.append(f"not answered: {missing}")
    return out


def build_payload(events: List[Dict[str, Any]], concerns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The gate agent's input for one page: labelled open concerns and labelled events."""
    return {
        "open_concerns": [{"label": f"C{i}", "title": c.get("title"), "subject": c.get("subject") or "household",
                           "status": c["_bucket"], "notes": c.get("notes") or ""}
                          for i, c in enumerate(concerns, 1)],
        "events": [_event_view(f"E{i}", e) for i, e in enumerate(events, 1)],
    }


def route_page(events: List[Dict[str, Any]], concerns: List[Dict[str, Any]],
               call: Callable[[Dict[str, Any]], Any]) -> Dict[int, Dict[str, Any]]:
    """Route one page. Returns event id -> {route, concern_ids, reasoning}; raises when the answer
    is still invalid after one correction."""
    ev_labels = [f"E{i}" for i in range(1, len(events) + 1)]
    c_labels = [f"C{i}" for i in range(1, len(concerns) + 1)]
    c_by_label = dict(zip(c_labels, concerns))
    payload = build_payload(events, concerns)
    data = call(payload)
    problems = _problems(data, ev_labels, c_labels)
    if problems:
        logger.warning("[gate] invalid routing, one correction: %s", problems)
        data = call({**payload, "correction": "; ".join(problems)})
        problems = _problems(data, ev_labels, c_labels)
        if problems:
            raise ValueError(f"gate routing still invalid after one correction: {problems}")
    by_label = dict(zip(ev_labels, events))
    out = {}
    for d in data["decisions"]:
        e = by_label[d["event"]]
        named = list(dict.fromkeys(d.get("concerns") or [])) if d["route"] == "concern" else []
        out[e["id"]] = {"route": d["route"], "concern_ids": [c_by_label[x]["concern_id"] for x in named],
                        "reasoning": str(d.get("reasoning") or "")}
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="gate", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::gate", actor_id="subconscious::gate")


def run_gate(*, ingest: bool = True, call: Optional[Callable[[Dict[str, Any]], Any]] = None,
             register: Optional[Dict[str, Any]] = None, trigger: Optional[Callable[[str], Any]] = None,
             connect=None) -> Dict[str, Any]:
    """Ingest new chat, route every pending event, trigger the noticer if anything was passed on.
    Free when nothing is pending (no model call). The keyword arguments are test seams."""
    from app.assistant.subconscious import brain_inbox as inbox
    from belief_engine.matching.context import pages

    kw = {"connect": connect} if connect else {}
    ingested = inbox.ingest_chat(**kw) if ingest else 0
    events = inbox.pending(**kw)
    if not events:
        return {"ingested": ingested, "routed": 0, "passed_on": 0, "failed": 0}
    if register is None:
        from app.assistant.subconscious.concern_store import load_register
        register = load_register()
    concerns = open_concerns(register)
    call = call or _agent_call

    routed = passed = failed = 0
    for page in pages(events, max_chars=_PAGE_CHARS):
        try:
            decisions = route_page(page, concerns, call)
        except Exception as exc:
            logger.error("[gate] %d event(s) could not be routed; the noticer reads them unrouted: %s",
                         len(page), exc, exc_info=True)
            inbox.record_failure([e["id"] for e in page], str(exc), **kw)
            failed += len(page)
            continue
        for event_id, d in decisions.items():
            inbox.record_route(event_id, d["route"], d["concern_ids"], d["reasoning"], **kw)
            routed += 1
            passed += d["route"] != "none"
    if passed or failed:
        (trigger or _trigger_noticer)(f"gate passed on {passed} event(s), {failed} unrouted")
    logger.info("[gate] ingested=%d routed=%d passed_on=%d failed=%d", ingested, routed, passed, failed)
    return {"ingested": ingested, "routed": routed, "passed_on": passed, "failed": failed}


def _trigger_noticer(reason: str) -> Any:
    from app.assistant.subconscious.answer_capture import trigger_noticer
    return trigger_noticer(reason)
