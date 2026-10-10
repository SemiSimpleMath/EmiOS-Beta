"""Owner-only /brain/flow page: the brain as a diagram of its stages, replayed one wake at a time
(2026-10-02, docs/design/brain_insight_2026-10-02.md).

Each wake of the brain is a run (subconscious/brain_trace.py): the model calls made during it carry its
id, and what code decided (an answer refused, a candidate folded into a concern, a stale reason, a
list change) is recorded as its steps. The page picks a run on a time scrubber, lights the stages it ran,
and opens a stage's calls and steps in time order. Until 2026-10-10 the diagram ended in the handoffs to dayflow's
steward and the steward's calls on them; both were retired (the brain's list is dayflow's input).

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
     "agents": ["dayflow_routine_writer", "subconscious::day_placements"],
     "role": "Judges each belief the owner approved for the calendar against today, read with the beliefs closest "
             "to it in meaning; code builds the day from those that apply, and the day's placements (one per thing "
             "the day holds, with every belief about it) become the day's calendar concerns."},
    {"id": "day_tasks", "title": "Day's tasks", "col": 6, "row": 1,
     "agents": ["subconscious::day_tasks"],
     "role": "The brain's list's one decider: for every open concern, the calendar's included, and for the day, "
             "whether the assistant has a part, what and when, reading the brain's situation (schedule, presence, "
             "health, chat, todos, work, reminders) and each planner's proposals. Dayflow makes the work from the "
             "list."},
    {"id": "day_record", "title": "Day's record (shadow)", "col": 2, "row": 3,
     "agents": ["subconscious::day_record"],
     "role": "Shadow only: applies the open concerns and the brain's decisions today to today's calendar and records "
             "what happened, each state and milestone with what showed it; compared with the daily context tracker. "
             "Feeds nothing yet."},
    {"id": "door", "title": "Concern door", "col": 2, "row": 2, "agents": ["subconscious::concern_door"],
     "role": "Every new concern enters here: a new matter, the same as an open or a closed concern, or the "
             "same as another candidate. Code assigns ids and folds a duplicate into the concern it matches."},
    {"id": "stories", "title": "Stories", "col": 3, "row": 3,
     "agents": ["subconscious::story_matter", "subconscious::story_concern", "subconscious::story_day"],
     "role": "Last in every wake: what changed is told to the owner in plain words, per matter read, per "
             "concern and per day, for the /mind page."},
    {"id": "concerns", "title": "Concerns", "col": 3, "row": 1, "rows": 2, "agents": [],
     "role": "The register: every open, resolved and dormant concern with its record, summary and plan."},
    {"id": "summary", "title": "Summaries", "col": 4, "row": 1, "agents": ["subconscious::concern_summary"],
     "role": "A concern whose notes grew long gets them folded into one summary; every reader gets the summary "
             "and the notes written since."},
    {"id": "plan", "title": "Concern planner", "col": 5, "row": 1,
     "agents": ["subconscious::concern_planner", "subconscious::concern_tasks"],
     "role": "For a concern whose record changed: a read-only planner looks things up with tools, then "
             "concern_tasks decides act now, hold until a date, or no action, and changes the concern's entries "
             "on the brain's list (new, modified or cancelled by id; the rest kept)."},
]
# from, to, label (empty: the main path; a label marks a side path, drawn dashed)
EDGES = [
    ("gate", "brain", ""), ("brain", "door", ""), ("brain", "concerns", ""),
    ("door", "concerns", ""), ("concerns", "summary", ""), ("summary", "plan", ""),
    ("plan", "day_tasks", "proposals"), ("day_pass", "concerns", "the day's calendar concerns"),
    ("day_tasks", "gate", "work outcomes come back as events"), ("concerns", "stories", ""),
    ("concerns", "day_record", "shadow: the day's record"),
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
    """One run: its steps and calls, each with the diagram node it belongs to; the concerns it touched, by id."""
    from app.assistant.subconscious import brain_trace
    run = brain_trace.get_run(run_id)
    for s in run["steps"]:
        s["node"] = s["stage"]
    for c in run["calls"]:
        c["node"] = AGENT_NODE.get(c["agent"], c["trace"].get("stage") or "")
    index = _concern_index()
    touched = {s["subject"] for s in run["steps"] if s["subject"] in index}
    touched |= {c["trace"].get("concern_id") for c in run["calls"] if c["trace"].get("concern_id") in index}
    run["concerns"] = {cid: index[cid] for cid in touched}
    return jsonify(run)
