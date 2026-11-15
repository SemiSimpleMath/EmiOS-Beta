"""The model calls of the new intake.

The models decide; nothing here writes. Each agent's rules for its answer live in its validate.py and a refused answer
is corrected once in conversation, then raises AgentOutputInvalid (agent_runtime/correction.py; until 2026-11-15 these
calls re-rendered the prompt with the problems tucked into the date or the new belief).
"""
from __future__ import annotations

from app.assistant.utils.pydantic_classes import Message

from belief_engine.intake.day_items import item_text
from belief_engine.intake.redact import redact

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


def extract_day(day: str, insights: list, provenance: dict, scope_ctx) -> list[dict]:
    """The day's beliefs, each resting on entries from its insight's sources, restored to their source refs
    (day_items.insights gives each insight with its sources, and the provenance of every source)."""
    if not insights:
        return []
    result = _call(EXTRACTOR, {"date": day, "insights": insights}, scope_ctx)
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
    result = _call(DEDUP, {"new_belief": new, "existing_beliefs": [view(b) for b in candidates]}, scope_ctx)
    v = result["verdict"]
    return {"verdict": v, "target": (result.get("target") or "").strip() if v != "new" else None,
            "sources": sorted(set(result.get("sources") or [])) if v in ("same", "contradicts") else [],
            "reasoning": result.get("reasoning")}


def draft_revision(belief: dict, new_atom: dict, day: str, scope_ctx) -> dict:
    """Draft the held belief's wording reconciled with a new belief that contradicts it, for the owner to apply in
    the morning review (owner, 2026-10-03: "maybe these will again be things you get to approve in the morning").
    Nothing is written here.

    Returns outcome/statement/kind/reasoning. `unchanged` means no rewrite reconciles them (the new belief was one
    occasion, or the two cannot both be said); the statement and kind are then the held ones. Until 2026-10-03 the
    same call rewrote the held belief at once: right in content (the switch from intermittent fasting to
    calorie-reduced meals, 2026-05-07, in four beliefs overnight), but with "until <date>" history clauses, and once
    a misread (B198). The prompt now writes the present state only."""
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
