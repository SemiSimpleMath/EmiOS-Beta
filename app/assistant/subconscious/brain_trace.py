"""Every model call the brain's agents make: the exact prompts sent and what came back (2026-09-30).

For the /brain page (routes/brain_debug.py): the owner wants to see, for each thing the brain
handled, what each agent was sent and what it answered. `LLMClient.call_structured_output` (the one
path every agent call takes) calls `record_call` after each call of a TRACED agent, with the
messages exactly as sent (system and user), the structured result or the error, the engine and
the duration.

Callers tag what a call was about with `trace(...)`, a context manager over a contextvar: the gate
per page (`stage=gate`, `event_ids`), the brain per matter (`stage=brain`, `event_ids`; the concern
door's calls inside it inherit this), the brief writer per concern (`stage=brief`, `concern_id`),
the noticer per tick. The steward's calls carry no tag; the page finds the one that handled a
handoff item by the item id in its user prompt.

Table `brain_calls` in emi.db. Recording is observability: a failed write is logged as an error
and does not fail the model call it records.
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
})

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

_trace: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("brain_trace", default={})


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)
        c.execute("CREATE INDEX IF NOT EXISTS brain_calls_at ON brain_calls(at)")


@contextmanager
def trace(**keys: Any) -> Iterator[None]:
    """Tag every traced call made inside the block with `keys` (merged over any outer tags)."""
    token = _trace.set({**_trace.get(), **keys})
    try:
        yield
    finally:
        _trace.reset(token)


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


def record_call(*, agent_name: str, messages: List[Dict[str, Any]], engine: Optional[str],
                started: datetime, result: Any = None, error: Optional[BaseException] = None,
                connect=None) -> None:
    """Store one call of a traced agent. Untraced agents are ignored."""
    if agent_name not in TRACED_AGENTS:
        return
    try:
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
                       json.dumps(_trace.get(), ensure_ascii=False, default=str)))
    except Exception:
        logger.error("[brain_trace] could not record a %s call", agent_name, exc_info=True)


# ── reading, for the page ────────────────────────────────────────────────────

def list_calls(*, agent: Optional[str] = None, limit: int = 200, connect=None) -> List[Dict[str, Any]]:
    """Newest first, without the prompt texts (sizes only)."""
    connect = connect or _connect
    ensure_schema(connect)
    where, params = ("WHERE agent = ?", [agent]) if agent else ("", [])
    with connect(False) as c:
        rows = c.execute(f"SELECT id, at, agent, engine, duration_ms, length(system), length(user), "
                         f"length(result), error, trace FROM brain_calls {where} ORDER BY at DESC LIMIT ?",
                         (*params, limit)).fetchall()
    return [{"id": r[0], "at": r[1], "agent": r[2], "engine": r[3], "duration_ms": r[4], "system_chars": r[5],
             "user_chars": r[6], "result_chars": r[7], "error": r[8], "trace": json.loads(r[9])} for r in rows]


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
