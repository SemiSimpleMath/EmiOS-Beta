"""The two model calls of the new intake, with the checks code enforces on their output.

The models decide; nothing here writes. A call whose output fails its checks gets exactly one
correction attempt naming the problems, then raises.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

from belief_engine.intake.day_items import item_text
from belief_engine.intake.redact import redact

logger = get_logger(__name__)

EXTRACTOR = "belief_engine::belief_extractor"
_SCOPE = {"durable_fact": "chronic", "stable_relationship": "chronic", "stable_preference": "chronic",
          "routine_pattern": "chronic", "episodic_context": "temporary", "transient_state": "temporary"}
DEDUP = "belief_engine::belief_dedup"
REVISE = "belief_engine::belief_revise"


def scope():
    from app.assistant.scope.loader import load_scope_for_source
    return load_scope_for_source(
        kind="subsystem", source_id="belief_engine", actor_id="belief_intake_replay",
        identity_overrides={"owner_id": "belief_engine", "surface": "pipeline",
                            "scope_id": "scope::belief_engine::intake_replay"})


def _call(name: str, payload: dict, scope_ctx) -> dict:
    from app.assistant.ServiceLocator.service_locator import DI
    agent = DI.agent_factory.create_agent(name)
    if agent is None:
        raise RuntimeError(f"Agent {name!r} not found")
    data = getattr(agent.action_handler(Message(agent_input=payload, scope_context=scope_ctx)), "data", None)
    if not isinstance(data, dict):
        raise RuntimeError(f"{name} returned no structured output")
    return data


def _extraction_problems(result: dict, insight_ns: set, n_items: int) -> list[str]:
    problems = []
    for b in result.get("beliefs") or []:
        if b["insight_ref"] not in insight_ns:
            problems.append(f"belief cites insight {b['insight_ref']}, not one of {sorted(insight_ns)}")
        refs = b.get("sources") or []
        bad = [r for r in refs if not 1 <= r <= n_items]
        if bad:
            problems.append(f"belief cites timeline items outside 1..{n_items}: {bad}")
        if len(set(refs)) != len(refs):
            problems.append("a belief cites the same item twice")
    return problems


def extract_day(day: str, insights: list, items: list, provenance: dict, scope_ctx) -> list[dict]:
    """The day's beliefs, each with its sources restored from the timeline."""
    if not insights:
        return []
    insight_ns = {i["n"] for i in insights}
    payload = {"date": day, "insights": insights, "timeline_items": items}
    result = _call(EXTRACTOR, payload, scope_ctx)
    problems = _extraction_problems(result, insight_ns, len(items))
    if problems:
        logger.warning("[intake] %s extraction invalid, one correction: %s", day, problems)
        result = _call(EXTRACTOR, {**payload, "date": f"{day} (correct these problems in your previous "
                                   f"answer: {'; '.join(problems)})"}, scope_ctx)
        problems = _extraction_problems(result, insight_ns, len(items))
        if problems:
            raise ValueError(f"{day} extraction still invalid after one correction: {problems}")
    beliefs = []
    for b in result["beliefs"]:
        sources = []
        for n in b["sources"]:
            p = provenance[n]
            it = p["item"]
            src = {"time": it.get("time"), "kind": p["kind"], "relation": "support",
                   "text": redact(item_text(it)), "source_ref": p["source_ref"]}
            if it.get("prompt"):
                src["in_reply_to"] = redact(it.get("prompt_text") or it.get("prompt"))
            sources.append(src)
        beliefs.append({"statement": redact(b["statement"]), "kind": b["kind"], "scope": _SCOPE[b["kind"]],
                        "insight_ref": b["insight_ref"], "reasoning": b.get("reasoning"), "sources": sources})
    return beliefs


def dedup(atom: dict, day: str, candidates: list[dict], scope_ctx) -> dict:
    """same / refines / contradicts / new against the candidates (already ordered closest first)."""
    if not candidates:
        return {"verdict": "new", "target": None, "reasoning": "No beliefs held yet."}
    by_id = {b["id"]: b for b in candidates}

    def view(b):
        v = {"id": b["id"], "statement": b["statement"], "kind": b["kind"],
             "sources": [{k: s[k] for k in ("day", "time", "kind", "text", "in_reply_to") if s.get(k)}
                         for s in b["sources"]]}
        if b.get("parent_id"):
            v["refines"] = by_id[b["parent_id"]]["statement"] if b["parent_id"] in by_id else b["parent_id"]
        return v

    new = {"statement": atom["statement"], "kind": atom["kind"],
           "sources": [{"n": n, "day": day, **{k: s[k] for k in ("time", "kind", "text", "in_reply_to") if s.get(k)}}
                       for n, s in enumerate(atom["sources"], 1)]}
    payload = {"new_belief": new, "existing_beliefs": [view(b) for b in candidates]}

    def problems(result: dict) -> list[str]:
        v, target, srcs = result.get("verdict"), (result.get("target") or "").strip(), result.get("sources") or []
        out = []
        if v not in ("same", "refines", "contradicts", "new"):
            out.append(f"verdict {v!r} is not one of same, refines, contradicts, new")
        elif v != "new" and target not in by_id:
            out.append(f"target {target!r} is not one of the listed ids")
        if v in ("same", "contradicts"):
            if not srcs:
                out.append(f"{v} names no bearing sources; give the numbers of the new belief's sources that bear on the target")
            bad = [n for n in srcs if not 1 <= n <= len(atom["sources"])]
            if bad:
                out.append(f"sources {bad} are outside 1..{len(atom['sources'])}")
        return out

    result = _call(DEDUP, payload, scope_ctx)
    found = problems(result)
    if found:
        logger.warning("[intake] dedup output invalid, one correction: %s", found)
        result = _call(DEDUP, {**payload, "new_belief": {**new, "note": (
            "Correct these problems in your previous answer: " + "; ".join(found))}}, scope_ctx)
        found = problems(result)
        if found:
            raise ValueError(f"dedup output still invalid after one correction: {found}")
    v = result["verdict"]
    return {"verdict": v, "target": (result.get("target") or "").strip() if v != "new" else None,
            "sources": sorted(set(result.get("sources") or [])) if v in ("same", "contradicts") else [],
            "reasoning": result.get("reasoning")}


def revise(belief: dict, new_atom: dict, day: str, scope_ctx) -> dict:
    """Restate a held belief after a new belief arrived that bears against it.

    Returns outcome/statement/kind/reasoning. `unchanged` means the belief stands as held (the new
    evidence confirmed it, asked a question, or was one occasion); the statement and kind are then
    the held ones, whatever the model wrote. The new belief is shown whole — statement, the kind
    the extractor gave it, its evidence — so a one-occasion exception and a new lasting state can
    be told apart.
    """
    held = {"statement": belief["statement"], "kind": belief["kind"]}
    def view(srcs):
        return [{k: s[k] for k in ("day", "time", "kind", "relation", "text", "in_reply_to") if s.get(k)} for s in srcs]
    evidence = sorted(belief["sources"], key=lambda s: (s.get("day") or "", s.get("time") or ""))
    new_belief = {"statement": new_atom["statement"], "kind": new_atom["kind"],
                  "evidence": view([{"day": day, **s} for s in new_atom["sources"]])}
    result = _call(REVISE, {"belief": held, "evidence": view(evidence), "new_belief": new_belief}, scope_ctx)
    outcome = result.get("outcome")
    if outcome == "unchanged":
        return {"outcome": outcome, "statement": belief["statement"], "kind": belief["kind"],
                "reasoning": result.get("reasoning")}
    statement = (result.get("statement") or "").strip()
    if outcome != "revised" or not statement or result.get("kind") not in _SCOPE:
        raise ValueError(f"revise returned an invalid outcome/statement/kind: {result!r}")
    return {"outcome": outcome, "statement": statement, "kind": result["kind"], "reasoning": result.get("reasoning")}
