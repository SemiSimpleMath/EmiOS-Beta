"""Owner-only /brain/flow page: the brain as a diagram of its stages, replayed one wake at a time
(2026-10-02, docs/design/brain_insight_2026-10-02.md).

Each wake of the brain is a run (subconscious/brain_trace.py): the model calls made during it carry its
id, and what code decided (an answer refused, a candidate folded into a concern, a stale reason, a
handoff) is recorded as its steps. The page picks a run on a time scrubber, lights the stages it ran,
and opens a stage's calls and steps in time order. The steward is dayflow's, not the brain's: its calls
for a run are the ones whose prompt holds an item the run handed over.

The diagram is NODES and EDGES below: the brain's stages are code in subconscious/, not a manager's
state_map, so unlike /dayflow/flow there is no config to read it from.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from flask import Blueprint, jsonify, render_template, request

from app.routes._security import reject_if_not_local
from app.services.llm_factory import resolve_tier

brain_flow_bp = Blueprint("brain_flow", __name__)
brain_flow_bp.before_request(reject_if_not_local)

_AGENTS = Path(__file__).resolve().parents[1] / "assistant" / "agents"

# col, row: the node's place in the diagram's grid; rows 2 holds what feeds the concerns from below.
NODES: List[Dict[str, Any]] = [
    {"id": "gate", "title": "Inbox & gate", "col": 1, "row": 1, "agents": ["subconscious::gate"],
     "role": "New chat messages and emails enter the inbox. The gate routes each ready event: to the open "
             "concerns it bears on, as a new matter, or nowhere. A chat room is read once it goes quiet."},
    {"id": "brain", "title": "Brain", "col": 2, "row": 1, "agents": ["subconscious::brain"],
     "role": "Reads one matter at a time with everything that bears on it, and decides: notes and "
             "resolutions on concerns, new concerns, and a decision on every event."},
    {"id": "day_pass", "title": "Day pass", "col": 1, "row": 2,
     "agents": ["dayflow_routine_writer"],
     "role": "Places each belief the owner approved for the calendar at its times today, read with the beliefs "
             "closest to it in meaning; code builds the day from the placements and hands each one to dayflow."},
    {"id": "day_decision", "title": "Day decision (shadow)", "col": 1, "row": 3,
     "agents": ["subconscious::day_decider"],
     "role": "Shadow only: decides what each placement on today's calendar needs (who acts, the assistant's part, "
             "when, or nothing and why) and records it beside what dayflow did. Nothing is handed over yet."},
    {"id": "concern_list", "title": "Concern list (shadow)", "col": 5, "row": 2,
     "agents": ["subconscious::concern_list_decider"],
     "role": "Shadow only: after the planner, keeps the concern's entries on the brain's list (keep, modify or "
             "cancel by id, add what is missing), each naming who acts, the assistant's part and when."},
    {"id": "door", "title": "Concern door", "col": 2, "row": 2, "agents": ["subconscious::concern_door"],
     "role": "Every new concern enters here: a new matter, the same as an open or a closed concern, or the "
             "same as another candidate. Code assigns ids and folds a duplicate into the concern it matches."},
    {"id": "stories", "title": "Stories", "col": 3, "row": 3,
     "agents": ["subconscious::story_matter", "subconscious::story_concern", "subconscious::story_day"],
     "role": "Last in every wake: what changed is told to the owner in plain words, per matter read, per "
             "concern and per day, for the /mind page."},
    {"id": "concerns", "title": "Concerns", "col": 3, "row": 1, "rows": 2, "agents": [],
     "role": "The register: every open, resolved and dormant concern with its record, brief and plan."},
    {"id": "brief", "title": "Briefs", "col": 4, "row": 1, "agents": ["subconscious::brief"],
     "role": "A concern whose record changed gets its brief written again: what it is, what is known, what "
             "was tried, what the owner wants."},
    {"id": "plan", "title": "Concern planner", "col": 5, "row": 1,
     "agents": ["subconscious::concern_planner", "subconscious::concern_decider"],
     "role": "For a matter concern with a new brief: a read-only planner looks things up with tools, then a "
             "decider writes act now, hold until a date, or no action, with the things that should be done."},
    {"id": "handoff", "title": "Handoffs", "col": 6, "row": 1, "agents": [],
     "role": "Code hands dayflow a concern whose plan says act now, and each placement on today's "
             "calendar at its time, as intake the steward must answer."},
    {"id": "steward", "title": "Steward (dayflow)", "col": 7, "row": 1,
     "agents": ["dayflow_orchestrator::strategic_planner_wo"],
     "role": "Turns each handoff into work or answers it as no action, with a reason. Work made from a "
             "handoff carries its concern or belief by code; every outcome comes back to the brain."},
]
# from, to, label (empty: the main path; a label marks a side path, drawn dashed)
EDGES = [
    ("gate", "brain", ""), ("brain", "door", ""), ("brain", "concerns", ""),
    ("door", "concerns", ""), ("concerns", "brief", ""), ("brief", "plan", ""),
    ("plan", "handoff", ""), ("day_pass", "handoff", "today's calendar"), ("handoff", "steward", ""),
    ("steward", "gate", "work outcomes come back as events"), ("concerns", "stories", ""),
    ("day_pass", "day_decision", "shadow: recorded, not handed over"),
    ("plan", "concern_list", "shadow: recorded, not handed over"),
]
AGENT_NODE = {a: n["id"] for n in NODES for a in n["agents"]}


def _engine(agent_name: str) -> Optional[str]:
    path = _AGENTS.joinpath(*agent_name.split("::")) / "config.yaml"
    if not path.exists():
        return None
    params = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("llm_params") or {}
    return resolve_tier(params["llm_provider"], params["model_tier"])[0] if params else None


@brain_flow_bp.route("/brain/flow")
def brain_flow_page():
    return render_template("brain_flow.html")


@brain_flow_bp.route("/api/brain/flow")
def brain_flow_api():
    return jsonify({"nodes": [{**n, "agents": [{"name": a, "engine": _engine(a)} for a in n["agents"]]} for n in NODES],
                    "edges": [{"from": a, "to": b, "label": label} for a, b, label in EDGES]})


@brain_flow_bp.route("/api/brain/runs")
def brain_runs_api():
    """Runs newest first: those that did something, or every wake with idle=1; concern= those touching it."""
    from app.assistant.subconscious import brain_trace
    limit = min(int(request.args.get("limit") or 300), 3000)
    return jsonify({"runs": brain_trace.list_runs(limit=limit, active_only=not request.args.get("idle"),
                                                  concern_id=(request.args.get("concern") or "").strip() or None)})


def _concern_index() -> Dict[str, Dict[str, Any]]:
    from app.assistant.subconscious.concern_store import load_register
    register = load_register()
    return {c["concern_id"]: {"title": c.get("title"), "status": bucket, "kind": c.get("kind")}
            for bucket in ("active", "addressing", "resolved", "dormant") for c in register.get(bucket) or []}


@brain_flow_bp.route("/api/brain/runs/<run_id>")
def brain_run_api(run_id: str):
    """One run: its steps and calls, each with the diagram node it belongs to; the steward's calls for the
    items it handed over; the concerns it touched, by id."""
    from app.assistant.subconscious import brain_trace
    run = brain_trace.get_run(run_id)
    for s in run["steps"]:
        s["node"] = s["stage"]
    for c in run["calls"]:
        c["node"] = AGENT_NODE.get(c["agent"], c["trace"].get("stage") or "")
    items = [s["detail"]["item_id"] for s in run["steps"] if s["stage"] == "handoff" and s["what"] == "handed"]
    steward: Dict[str, Dict[str, Any]] = {}
    for item in items:
        for call in brain_trace.steward_calls_mentioning(item):
            steward.setdefault(call["id"], {**call, "node": "steward", "items": []})["items"].append(item)
    run["calls"] += sorted(steward.values(), key=lambda c: c["at"])
    index = _concern_index()
    touched = {s["subject"] for s in run["steps"] if s["subject"] in index}
    touched |= {c["trace"].get("concern_id") for c in run["calls"] if c["trace"].get("concern_id") in index}
    run["concerns"] = {cid: index[cid] for cid in touched}
    return jsonify(run)
