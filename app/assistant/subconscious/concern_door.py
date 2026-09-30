"""The one way a concern enters the register (2026-09-30).

Every source that raises a concern (today the noticer; next the brain step, chat and dayflow)
hands its candidates here. Before this, the noticer wrote them straight in, under ids it made up,
and the only duplicate check was an anchor the model almost never filled: the 60 most recent
concerns held four for one makeup picture day, three for one school evening, and about six
variants of the same sleep pattern.

Two phases, so the model call never runs under the register lock:

1. `plan_admission` (no lock). A candidate whose anchor the owner already settled (declined, or
   accepted as chronic) is suppressed by code. The rest go to `subconscious::concern_door`, which
   sees them beside every open concern, every concern resolved in the last CLOSED_WINDOW, and every
   dormant one (dormant concerns carry the owner's rulings), and decides for each candidate: a new
   matter, the same matter as an open concern, the same as a closed one, or the same as another
   candidate in this batch. Labels are request-local (N1… candidates, C1… concerns) and mapped back
   by code. One correction round; still invalid raises, so nothing is written and the source's
   inputs stay available for its next run.
2. `apply_admission` (under the lock). Code assigns every new concern's id and records its origin
   and time. A duplicate is folded into the concern it matches: its evidence appended and the merge
   journalled there, never a second concern. A candidate matching a closed concern is journalled on
   that concern and not admitted.

Every candidate states `done_when`: the outcome that closes it. It is required here, not only in a
model's schema, because every closing path measures against it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "subconscious::concern_door"
CLOSED_WINDOW = timedelta(days=14)
_DECISIONS = ("new", "same_open", "same_closed", "same_candidate")


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _comparable(register: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """Open concerns, concerns resolved within CLOSED_WINDOW, and every dormant concern."""
    out = [{**c, "_status": b} for b in ("active", "addressing") for c in register.get(b) or []]
    for c in register.get("resolved") or []:
        when = _parse(c.get("resolved_at_utc"))
        if when is not None and now - when <= CLOSED_WINDOW:
            out.append({**c, "_status": "resolved"})
    out += [{**c, "_status": "dormant"} for c in register.get("dormant") or []]
    return out


def _local_date(value: Any) -> str:
    from app.assistant.utils.time_utils import utc_to_local
    when = _parse(value)
    return utc_to_local(when).strftime("%a %Y-%m-%d") if when else ""


def build_payload(candidates: List[Dict[str, Any]], existing: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The judge's input: labelled candidates and labelled concerns, every field it compares on."""
    def closed_because(c: Dict[str, Any]) -> str:
        if c.get("user_declined_at_utc"):
            return "the owner declined it"
        if c.get("chronic"):
            return f"accepted as chronic: {c.get('dormant_reason') or ''}".strip()
        return str(c.get("resolution_reason") or c.get("dormant_reason") or "")

    return {
        "candidates": [{
            "label": f"N{i}", "title": c.get("title"), "subject": c.get("subject") or "household",
            "kind": c.get("kind"), "horizon": c.get("horizon"), "anchor": c.get("anchor"),
            "done_when": c.get("done_when"), "notes": c.get("notes") or "",
            "evidence": [{"kind": e.get("kind"), "ref": e.get("ref"), "snippet": e.get("snippet")}
                         for e in c.get("evidence") or []],
        } for i, c in enumerate(candidates, 1)],
        "existing": [{
            "label": f"C{i}", "status": c["_status"], "title": c.get("title"),
            "subject": c.get("subject") or "household", "kind": c.get("kind"), "anchor": c.get("anchor"),
            "done_when": c.get("done_when"), "notes": c.get("notes") or "",
            "first_observed": c.get("first_observed"),
            "closed_on": _local_date(c.get("resolved_at_utc") or c.get("dormant_at_utc")),
            "closed_because": closed_because(c) if c["_status"] in ("resolved", "dormant") else "",
            "journal": c.get("reinforcement_notes") or "",
        } for i, c in enumerate(existing, 1)],
    }


def _problems(data: Any, cand_labels: List[str], open_labels: List[str], closed_labels: List[str]) -> List[str]:
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        return ["no decisions list"]
    out, seen, by_label = [], [], {}
    for d in data["decisions"]:
        label = str((d or {}).get("candidate") or "").strip()
        if label not in cand_labels:
            out.append(f"candidate {label!r} is not one of the candidates given")
            continue
        seen.append(label)
        by_label[label] = d
    for label, d in by_label.items():
        decision, target = d.get("decision"), str(d.get("same_as") or "").strip()
        if decision not in _DECISIONS:
            out.append(f"{label}: decision {decision!r} is not one of {list(_DECISIONS)}")
        elif decision == "new" and target:
            out.append(f"{label}: a new matter names no same_as")
        elif decision == "same_open" and target not in open_labels:
            out.append(f"{label}: same_open must name an open concern label, got {target!r}")
        elif decision == "same_closed" and target not in closed_labels:
            out.append(f"{label}: same_closed must name a closed concern label, got {target!r}")
        elif decision == "same_candidate":
            if target == label or target not in cand_labels:
                out.append(f"{label}: same_candidate must name another candidate, got {target!r}")
            elif (by_label.get(target) or {}).get("decision") != "new":
                out.append(f"{label}: same_candidate must name a candidate decided new, {target} is not")
    dup = sorted({x for x in seen if seen.count(x) > 1})
    if dup:
        out.append(f"answered more than once: {dup}")
    missing = [x for x in cand_labels if x not in seen]
    if missing:
        out.append(f"not answered: {missing}")
    return out


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.routine_handlers.subconscious import _run_subconscious_agent
    return _run_subconscious_agent(handler_label="concern_door", agent_name=_AGENT, context=payload,
                                   scope_id="subconscious::concern_door", actor_id="subconscious::concern_door")


def plan_admission(candidates: List[Dict[str, Any]], register: Dict[str, Any], *,
                   judge: Optional[Callable[[Dict[str, Any]], Any]] = None,
                   now_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Decide, for each candidate, whether it is a new concern or one the register already holds.

    Returns one step per candidate, in the candidates' order:
    {candidate, action: create | merge_open | suppress_closed | merge_candidate, target, reason}.
    `target` is the concern_id matched (merge_open, suppress_closed) or the index of the candidate
    it joins (merge_candidate). Raises on a candidate without title or done_when, and when the
    judge's answer is still invalid after one correction."""
    from app.assistant.subconscious.persist import _settled_anchors
    for c in candidates:
        if not str(c.get("title") or "").strip() or not str(c.get("done_when") or "").strip():
            raise ValueError(f"a concern candidate needs a title and done_when: {c.get('label')!r}")
    now = now_utc or datetime.now(timezone.utc)
    settled = _settled_anchors(register)
    steps: List[Optional[Dict[str, Any]]] = [None] * len(candidates)
    judged: List[int] = []
    for i, c in enumerate(candidates):
        prior = settled.get(str(c.get("anchor") or "").strip()) if c.get("anchor") else None
        if prior is not None:
            ruling = "declined" if prior.get("user_declined_at_utc") else "accepted as chronic"
            steps[i] = {"candidate": c, "action": "suppress_closed", "target": prior["concern_id"],
                        "reason": ruling}
        else:
            judged.append(i)

    existing = _comparable(register, now)
    if judged and not existing and len(judged) == 1:
        steps[judged[0]] = {"candidate": candidates[judged[0]], "action": "create", "target": None,
                            "reason": "nothing to compare with"}
    elif judged:
        batch = [candidates[i] for i in judged]
        cand_labels = [f"N{k}" for k in range(1, len(batch) + 1)]
        ex_labels = [f"C{k}" for k in range(1, len(existing) + 1)]
        open_labels = [lab for lab, c in zip(ex_labels, existing) if c["_status"] in ("active", "addressing")]
        closed_labels = [lab for lab in ex_labels if lab not in open_labels]
        payload = build_payload(batch, existing)
        call = judge or _agent_call
        data = call(payload)
        problems = _problems(data, cand_labels, open_labels, closed_labels)
        if problems:
            logger.warning("[concern_door] invalid decisions, one correction: %s", problems)
            data = call({**payload, "correction": "; ".join(problems)})
            problems = _problems(data, cand_labels, open_labels, closed_labels)
            if problems:
                raise ValueError(f"concern_door decisions still invalid after one correction: {problems}")
        by_ex = dict(zip(ex_labels, existing))
        by_cand = dict(zip(cand_labels, judged))
        for d in data["decisions"]:
            i = by_cand[d["candidate"]]
            target = str(d.get("same_as") or "").strip()
            action = {"new": "create", "same_open": "merge_open", "same_closed": "suppress_closed",
                      "same_candidate": "merge_candidate"}[d["decision"]]
            steps[i] = {"candidate": candidates[i], "action": action, "reason": str(d.get("reason") or ""),
                        "target": (by_ex[target]["concern_id"] if action in ("merge_open", "suppress_closed")
                                   else by_cand[target] if action == "merge_candidate" else None)}
    return steps  # type: ignore[return-value]


def _find(register: Dict[str, Any], concern_id: str) -> Dict[str, Any]:
    for bucket in ("active", "addressing", "resolved", "dormant"):
        for c in register.get(bucket) or []:
            if c.get("concern_id") == concern_id:
                return c
    raise KeyError(f"concern {concern_id} is no longer in the register")


def apply_admission(steps: List[Dict[str, Any]], register: Dict[str, Any], *, source: str,
                    now_iso: str) -> Dict[str, Optional[str]]:
    """Apply a plan to the register (the caller holds the register lock and saves).

    Returns candidate label -> the concern_id it now lives in, or None when it was suppressed, so a
    source's other references to the label (a question about a concern it just raised) follow it."""
    from app.assistant.subconscious import persist
    landed: Dict[int, str] = {}
    labels: Dict[str, Optional[str]] = {}

    for i, step in enumerate(steps):
        if step["action"] != "create":
            continue
        record = {k: v for k, v in step["candidate"].items() if k != "label"}
        record.update({"concern_id": str(uuid.uuid4()), "origin": source, "created_at_utc": now_iso})
        register.setdefault("active", []).append(record)
        landed[i] = record["concern_id"]

    for i, step in enumerate(steps):
        c, action = step["candidate"], step["action"]
        title = str(c.get("title") or "")
        if action == "create":
            pass
        elif action in ("merge_open", "merge_candidate"):
            target_id = step["target"] if action == "merge_open" else landed[step["target"]]
            target = _find(register, target_id)
            target.setdefault("evidence", []).extend(c.get("evidence") or [])
            persist._bump_reinforcement_count(target)
            target["last_reinforced_utc"] = now_iso
            persist._journal_on(target, now_iso, f"MERGED a re-raise from {source}: {title} ({step['reason']})")
            persist._trim_evidence(target)
            landed[i] = target_id
        elif action == "suppress_closed":
            prior = _find(register, step["target"])
            persist._journal_on(prior, now_iso, f"SUPPRESSED a re-mint of this ({step['reason']}): {title}")
            prior["suppressed_remint_count"] = int(prior.get("suppressed_remint_count") or 0) + 1
            prior["suppressed_remint_at_utc"] = now_iso
        else:
            raise ValueError(f"unknown admission action {action!r}")
        logger.info("[concern_door] %s %r -> %s (%s)", action, title, landed.get(i) or step.get("target"),
                    step["reason"])
        if c.get("label"):
            labels[str(c["label"])] = landed.get(i)
    return labels
