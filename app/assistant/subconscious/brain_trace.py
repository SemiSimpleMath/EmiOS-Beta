"""Every model call the brain's agents make: the exact prompts sent and what came back (2026-09-30).

For the /brain page (routes/brain_debug.py): the owner wants to see, for each thing the brain
handled, what each agent was sent and what it answered. `LLMClient.call_structured_output` (the one
path every agent call takes) calls `record_call` after each call of a TRACED agent, with the
messages exactly as sent (system and user), the structured result or the error, the engine and
the duration.

For the /dayflow/flow page (routes/dayflow_flow.py, 2026-09-30) it also records every
`dayflow_orchestrator::` agent, and every agent called inside a dayflow work attempt (the worker's
agents, the ticket builder), tagged with the attempt's work_id and main node. Those are kept to the
newest KEEP_PER_AGENT calls per agent; the brain's agents keep their whole history.

It also records REPLAY_AGENTS (2026-10-01), likewise keeping the newest KEEP_PER_AGENT: the belief and
knowledge-graph agents and the highest-volume agents, so a model or reasoning-effort change can be
replayed on their real prompts before it ships (scratch/replay/).

Callers tag what a call was about with `trace(...)`, a context manager over a contextvar: the gate
per page (`stage=gate`, `event_ids`), the brain per matter (`stage=brain`, `event_ids`; the concern
door's calls inside it inherit this), the brief writer per concern (`stage=brief`, `concern_id`),
the noticer per tick. The steward's calls carry no tag; the page finds the one that handled a
handoff item by the item id in its user prompt.

Runs and steps (2026-10-02, docs/design/brain_insight_2026-10-02.md): each wake of the brain is a run
(`run`, table `brain_runs`); its id rides in the same contextvar, so every call made during the wake
carries `run_id`. What code decides (an answer refused, a candidate suppressed, a stale reason, a
handoff held back, a tool's result) is recorded where it is decided with `step` (table `brain_steps`),
so the /brain/flow page can replay a run stage by stage without reading the logs.

Tables `brain_calls`, `brain_runs` and `brain_steps` in emi.db. Recording is observability: a failed
write is logged as an error and does not fail the model call or the decision it records.
"""
from __future__ import annotations

import contextvars
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

TRACED_AGENTS = frozenset({
    "subconscious::gate", "subconscious::brain", "subconscious::concern_door", "subconscious::brief",
    "subconscious::noticer", "dayflow_orchestrator::strategic_planner_wo",
    "subconscious::concern_planner", "subconscious::concern_tasks",
    "dayflow_routine_writer",   # the day pass
    "subconscious::day_tasks",     # the day's tasks, shadow only (subconscious/day_tasks.py)
    "subconscious::story_matter", "subconscious::story_concern", "subconscious::story_day",
})

REPLAY_AGENTS = frozenset({
    "belief_engine::belief_dedup", "belief_engine::evidence_page", "belief_engine::match_review",
    "belief_engine::evidence_match", "belief_engine::belief_extractor", "belief_engine::merge_verifier",
    "belief_engine::merge_check", "belief_engine::match_discover", "belief_engine::belief_atomizer",
    "dayflow_cron_tickets", "daily_context_tracker",
    "health_status_inference", "label", "email_parser", "subconscious::answer_matcher",
    "knowledge_graph_add::fact_extractor", "knowledge_graph_add::entity_resolver", "knowledge_graph_add::node_merger",
    "kg_maintenance::succession_judge", "kg_maintenance::arrangement_judge", "kg_maintenance::identity_sentence_writer",
    "kg_maintenance::duplicate_cluster_resolver", "kg_maintenance::duplicate_detector",
    "kg_finding_cluster_resolver", "wiki_consistency_critic",
})

KEEP_PER_AGENT = 50          # calls kept per agent recorded for the dayflow page and replay

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_calls (
    id TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    agent TEXT NOT NULL,
    engine TEXT,
    duration_ms INTEGER,
    system TEXT NOT NULL,
    user TEXT NOT NULL,
    result TEXT,
    error TEXT,
    trace TEXT NOT NULL)"""

RUNS_SCHEMA = """CREATE TABLE IF NOT EXISTS brain_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    trigger TEXT NOT NULL,
    summary TEXT,
    error TEXT)"""
# trigger: boot | event | timer

STEPS_SCHEMA = """CREATE TABLE IF NOT EXISTS brain_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    at TEXT NOT NULL,
    stage TEXT NOT NULL,
    what TEXT NOT NULL,
    subject TEXT,
    detail TEXT)"""
# stage: gate | brain | door | brief | plan | handoff | day_pass | stories. subject: a concern id, an item id, or
# event ids as JSON. detail: JSON.

_trace: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("brain_trace", default={})


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)
        c.execute("CREATE INDEX IF NOT EXISTS brain_calls_at ON brain_calls(at)")
        c.execute(RUNS_SCHEMA)
        c.execute("CREATE INDEX IF NOT EXISTS brain_runs_started ON brain_runs(started_at)")
        c.execute(STEPS_SCHEMA)
        c.execute("CREATE INDEX IF NOT EXISTS brain_steps_run ON brain_steps(run_id)")
        c.execute("CREATE INDEX IF NOT EXISTS brain_steps_subject ON brain_steps(subject)")


@contextmanager
def trace(**keys: Any) -> Iterator[None]:
    """Tag every traced call made inside the block with `keys` (merged over any outer tags)."""
    token = _trace.set({**_trace.get(), **keys})
    try:
        yield
    finally:
        _trace.reset(token)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def run(trigger: str, *, connect=None) -> Iterator[Dict[str, Any]]:
    """One wake of the brain. Yields a dict: set `summary` on it before the block ends. Every call and
    step made inside carries the run's id; a run that raises is recorded with its error."""
    connect = connect or _connect
    run_id, outcome = uuid.uuid4().hex, {"summary": None}
    try:
        ensure_schema(connect)
        with connect(True) as c:
            c.execute("INSERT INTO brain_runs (id, started_at, trigger) VALUES (?, ?, ?)", (run_id, _now(), trigger))
    except Exception:
        logger.error("[brain_trace] could not record the start of a run", exc_info=True)
    token = _trace.set({**_trace.get(), "run_id": run_id})
    error = None
    try:
        yield outcome
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _trace.reset(token)
        try:
            with connect(True) as c:
                c.execute("UPDATE brain_runs SET ended_at = ?, summary = ?, error = ? WHERE id = ?",
                          (_now(), json.dumps(outcome["summary"], ensure_ascii=False, default=str), error, run_id))
        except Exception:
            logger.error("[brain_trace] could not record the end of run %s", run_id, exc_info=True)


def current(key: str) -> Any:
    """A tag of the current block (`trace`, `run`): run_id, stage, concern_id, ..."""
    return _trace.get().get(key)


def step(stage: str, what: str, *, subject: Any = None, connect=None, **detail: Any) -> None:
    """Record a decision code made, in the current run. Outside a wake (the noticer, tests) nothing is
    recorded."""
    run_id = _trace.get().get("run_id")
    if run_id is None:
        return
    try:
        connect = connect or _connect
        ensure_schema(connect)
        with connect(True) as c:
            c.execute("INSERT INTO brain_steps (run_id, at, stage, what, subject, detail) VALUES (?, ?, ?, ?, ?, ?)",
                      (run_id, _now(), stage, what,
                       subject if subject is None or isinstance(subject, str) else json.dumps(subject),
                       json.dumps(detail, ensure_ascii=False, default=str)))
    except Exception:
        logger.error("[brain_trace] could not record a %s step: %s", stage, what, exc_info=True)


def _text(messages: List[Dict[str, Any]], role: str) -> str:
    parts = []
    for m in messages or []:
        if m.get("role") != role:
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text") or f"[{p.get('type')}]" if isinstance(p, dict) else str(p)
                                for p in content)
        parts.append(str(content or ""))
    return "\n\n".join(parts)


def _jsonable(result: Any) -> Any:
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return result


def _work_tags() -> Dict[str, Any]:
    """The dayflow work attempt this call runs in (manager_runtime.execution), or {}."""
    from app.assistant.manager_runtime.execution import current_owner
    owner = current_owner()
    return {"work_id": owner.work_id, "work_node": owner.main_node_id} if owner is not None else {}


def record_call(*, agent_name: str, messages: List[Dict[str, Any]], engine: Optional[str],
                started: datetime, result: Any = None, error: Optional[BaseException] = None,
                connect=None) -> None:
    """Store one call of a traced agent: a brain agent, a dayflow agent, an agent called inside a
    dayflow work attempt, or a replay agent. Other agents are ignored."""
    try:
        work = _work_tags()
        brain = agent_name in TRACED_AGENTS
        if not (brain or agent_name.startswith("dayflow_orchestrator::") or work or agent_name in REPLAY_AGENTS):
            return
        connect = connect or _connect
        ensure_schema(connect)
        now = datetime.now(timezone.utc)
        with connect(True) as c:
            c.execute("INSERT INTO brain_calls (id, at, agent, engine, duration_ms, system, user, result, error, trace) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (uuid.uuid4().hex, started.isoformat(), agent_name, engine,
                       int((now - started).total_seconds() * 1000), _text(messages, "system"), _text(messages, "user"),
                       json.dumps(_jsonable(result), ensure_ascii=False, default=str) if error is None else None,
                       f"{type(error).__name__}: {error}" if error is not None else None,
                       json.dumps({**_trace.get(), **work}, ensure_ascii=False, default=str)))
            if not brain:
                c.execute("DELETE FROM brain_calls WHERE agent = ? AND id NOT IN "
                          "(SELECT id FROM brain_calls WHERE agent = ? ORDER BY at DESC LIMIT ?)",
                          (agent_name, agent_name, KEEP_PER_AGENT))
    except Exception:
        logger.error("[brain_trace] could not record a %s call", agent_name, exc_info=True)


# ── reading, for the page ────────────────────────────────────────────────────

def list_calls(*, agent: Optional[str] = None, agents: Optional[frozenset] = None, limit: int = 200,
               connect=None) -> List[Dict[str, Any]]:
    """Newest first, without the prompt texts (sizes only): one agent's calls, those of `agents`, or all."""
    connect = connect or _connect
    ensure_schema(connect)
    if agent:
        where, params = "WHERE agent = ?", [agent]
    elif agents:
        where, params = f"WHERE agent IN ({','.join('?' * len(agents))})", sorted(agents)
    else:
        where, params = "", []
    with connect(False) as c:
        rows = c.execute(f"SELECT {_META} FROM brain_calls {where} ORDER BY at DESC LIMIT ?",
                         (*params, limit)).fetchall()
    return [_meta(r) for r in rows]


_META = "id, at, agent, engine, duration_ms, length(system), length(user), length(result), error, trace"


def _meta(r) -> Dict[str, Any]:
    return {"id": r[0], "at": r[1], "agent": r[2], "engine": r[3], "duration_ms": r[4], "system_chars": r[5],
            "user_chars": r[6], "result_chars": r[7], "error": r[8], "trace": json.loads(r[9])}


def get_call(call_id: str, connect=None) -> Dict[str, Any]:
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        r = c.execute("SELECT id, at, agent, engine, duration_ms, system, user, result, error, trace "
                      "FROM brain_calls WHERE id = ?", (call_id,)).fetchone()
    if r is None:
        raise KeyError(call_id)
    return {"id": r[0], "at": r[1], "agent": r[2], "engine": r[3], "duration_ms": r[4], "system": r[5],
            "user": r[6], "result": json.loads(r[7]) if r[7] else None, "error": r[8], "trace": json.loads(r[9])}


def work_attempt_calls(*, exclude_prefix: str = "dayflow_orchestrator::", limit: int = 30,
                       connect=None) -> List[Dict[str, Any]]:
    """Newest calls made inside dayflow work attempts by agents outside `exclude_prefix` (the worker's
    agents, the ticket builder), without prompt texts."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        ids = [r[0] for r in c.execute(
            "SELECT id FROM brain_calls WHERE json_extract(trace, '$.work_id') IS NOT NULL "
            "AND agent NOT LIKE ? ORDER BY at DESC LIMIT ?", (exclude_prefix + "%", limit))]
    wanted = set(ids)
    return [c for c in list_calls(limit=100000, connect=connect) if c["id"] in wanted]


def calls_for_events(event_ids: List[int], connect=None) -> List[Dict[str, Any]]:
    """Every traced call tagged with any of these events (gate pages, the brain, the door), oldest first."""
    wanted = set(event_ids)
    return sorted([c for c in list_calls(limit=100000, connect=connect)
                   if wanted & set(c["trace"].get("event_ids") or [])], key=lambda c: c["at"])


def calls_for_concern(concern_id: str, connect=None) -> List[Dict[str, Any]]:
    """The brief writer's calls for this concern, oldest first."""
    return sorted([c for c in list_calls(agent="subconscious::brief", limit=100000, connect=connect)
                   if c["trace"].get("concern_id") == concern_id], key=lambda c: c["at"])


def steward_calls_mentioning(text: str, connect=None) -> List[Dict[str, Any]]:
    """Steward calls whose user prompt contains `text` (a handoff item id), oldest first."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM brain_calls WHERE agent = ? AND instr(user, ?) > 0 ORDER BY at",
                                       ("dayflow_orchestrator::strategic_planner_wo", text))]
    wanted = set(ids)
    return [c for c in list_calls(agent="dayflow_orchestrator::strategic_planner_wo", limit=100000, connect=connect)
            if c["id"] in wanted][::-1]


def _run_row(r) -> Dict[str, Any]:
    return {"id": r[0], "started_at": r[1], "ended_at": r[2], "trigger": r[3],
            "summary": json.loads(r[4]) if r[4] else None, "error": r[5]}


def list_runs(*, limit: int = 200, active_only: bool = True, concern_id: Optional[str] = None,
              connect=None) -> List[Dict[str, Any]]:
    """Runs newest first, each with how many steps and calls it holds. `active_only`: runs that recorded
    a step or a call (an idle wake records neither). `concern_id`: runs with a step about it or a call
    tagged with it."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        steps = dict(c.execute("SELECT run_id, count(*) FROM brain_steps WHERE run_id IS NOT NULL GROUP BY run_id"))
        calls = dict(c.execute("SELECT json_extract(trace, '$.run_id') AS r, count(*) FROM brain_calls "
                               "WHERE r IS NOT NULL GROUP BY r"))
        wanted = None
        if concern_id:
            wanted = {r[0] for r in c.execute("SELECT DISTINCT run_id FROM brain_steps WHERE subject = ?", (concern_id,))}
            wanted |= {r[0] for r in c.execute("SELECT DISTINCT json_extract(trace, '$.run_id') FROM brain_calls "
                                                "WHERE json_extract(trace, '$.concern_id') = ?", (concern_id,))}
        rows = c.execute("SELECT id, started_at, ended_at, trigger, summary, error FROM brain_runs "
                         "ORDER BY started_at DESC").fetchall()
    out = []
    for r in rows:
        if wanted is not None and r[0] not in wanted:
            continue
        n_steps, n_calls = steps.get(r[0], 0), calls.get(r[0], 0)
        if active_only and not (n_steps or n_calls or r[5]):
            continue
        out.append({**_run_row(r), "steps": n_steps, "calls": n_calls})
        if len(out) >= limit:
            break
    return out


def get_run(run_id: str, connect=None) -> Dict[str, Any]:
    """One run: its row, its steps and its calls (without prompt texts), each in time order."""
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        r = c.execute("SELECT id, started_at, ended_at, trigger, summary, error FROM brain_runs WHERE id = ?",
                      (run_id,)).fetchone()
        if r is None:
            raise KeyError(run_id)
        steps = [{"id": s[0], "at": s[1], "stage": s[2], "what": s[3], "subject": s[4],
                  "detail": json.loads(s[5]) if s[5] else {}}
                 for s in c.execute("SELECT id, at, stage, what, subject, detail FROM brain_steps WHERE run_id = ? "
                                    "ORDER BY at, id", (run_id,))]
        calls = [_meta(x) for x in c.execute(f"SELECT {_META} FROM brain_calls WHERE json_extract(trace, '$.run_id') = ? "
                                             "ORDER BY at", (run_id,))]
    return {**_run_row(r), "steps": steps, "calls": calls}
