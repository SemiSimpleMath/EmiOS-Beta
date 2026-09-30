"""Beliefs for a chat turn: the few that bear on what was just said.

Reads the intake's own store (belief_intake_* — the new pipeline's beliefs, one claim each, with
dated evidence). Two lanes: closeness in meaning to the message (cosine over the stored statement
embeddings, weighted a little by how recently and how often the belief was observed, the same
terms as belief_engine.retrieval) and the people the message names (a belief naming one of them is
lifted). The pick-from-candidates step is `rank`, a pure function, so a per-candidate classifier
can replace the vector cutoff later without touching the callers.

Every surfacing is logged (belief_intake_surfaced) so the useful k, the never-surfaced and the
surfaced-then-contradicted can be measured before anything is tuned.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from datetime import date, datetime
from typing import Any

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

# Closeness in meaning decides; recency and frequency only separate beliefs that are equally close.
# (belief_engine.retrieval's 0.55/0.25/0.20 assumed a tag scope had narrowed the set first; over
# the whole catalog those weights let a belief seen yesterday outrank the one the message is about.)
_WEIGHTS = {"relevance": 1.0, "recency": 0.06, "frequency": 0.04}
_RECENCY_HALF_LIFE_DAYS = 30.0
_FREQ_SATURATION = 20.0
_NAME_LIFT = 0.15          # added to the score of a belief naming a person the message names
# Below this cosine a belief is about something else (measured 2026-09-27 on the 533-belief store:
# the beliefs a message is about sit at 0.37–0.86; the tail at 0.24–0.30 is topic-adjacent noise).
# k is a ceiling, not a quota: a turn with nothing relevant shows nothing.
_MIN_RELEVANCE = 0.30
_CACHE_TTL_S = 300.0       # the store changes nightly; a turn never waits on a full reload

_SURFACED_SCHEMA = """CREATE TABLE IF NOT EXISTS belief_intake_surfaced (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, room_id TEXT, message_id TEXT,
    belief_id TEXT NOT NULL, rank INTEGER NOT NULL, score REAL NOT NULL, query TEXT)"""

_cache: dict[str, Any] = {"loaded_at": 0.0, "beliefs": []}


def _load_beliefs() -> list[dict]:
    """Every intake belief with its vector, first/last support day and support count."""
    from belief_engine.intake.store import app_db
    with app_db()(False) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, statement, kind, created_day, embedding FROM belief_intake_beliefs WHERE status='active' ORDER BY rowid")]
        support = {r["belief_id"]: (r["first"], r["last"], r["n"]) for r in c.execute(
            "SELECT belief_id, MIN(day) first, MAX(day) last, COUNT(*) n FROM belief_intake_evidence "
            "WHERE relation='support' GROUP BY belief_id")}
    for b in rows:
        b["embedding"] = json.loads(b.pop("embedding"))
        first, last, n = support.get(b["id"], (b["created_day"], b["created_day"], 0))
        b["first_day"], b["last_day"], b["support"] = first, last, n
    return rows


def beliefs() -> list[dict]:
    now = time.monotonic()
    if now - _cache["loaded_at"] > _CACHE_TTL_S:
        _cache["beliefs"] = _load_beliefs()
        _cache["loaded_at"] = now
    return _cache["beliefs"]


def _recency(last_day: str | None, today: date) -> float:
    if not last_day:
        return 0.5
    age = max(0.0, (today - date.fromisoformat(last_day)).days)
    return 0.5 ** (age / _RECENCY_HALF_LIFE_DAYS)


def _frequency(n: int) -> float:
    return min(1.0, math.log1p(float(n)) / math.log1p(_FREQ_SATURATION))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return max(0.0, dot / (na * nb))


def rank(candidates: list[dict], query_vector: list[float], names: list[str], k: int, today: date) -> list[dict]:
    """The k beliefs that bear most on the message. Pure: candidates carry embedding, last_day, support."""
    name_res = [re.compile(rf"\b{re.escape(n)}\b", re.I) for n in names if n]
    scored = []
    for b in candidates:
        rel = _cosine(query_vector, b["embedding"])
        if rel < _MIN_RELEVANCE:
            continue
        score = (_WEIGHTS["relevance"] * rel + _WEIGHTS["recency"] * _recency(b["last_day"], today)
                 + _WEIGHTS["frequency"] * _frequency(b["support"]))
        if any(r.search(b["statement"]) for r in name_res):
            score += _NAME_LIFT
        scored.append({**b, "score": score, "relevance": rel})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]


def expand_query(message: str) -> tuple[str, list[str]]:
    """(query text, entity names) for a chat message.

    A message and the beliefs about the same thing often name it differently — "the dogs" in chat,
    "Bonnie and Clyde" in the beliefs — and the embedding cannot bridge that: for "When do I usually
    take the dogs out?" the evening-walk belief scored 0.09 and two trash beliefs ("take the bins
    out") got in instead (2026-09-28). The KG entity detector the chat gate already uses finds the
    entities the message names; their one-line card summaries join the query so it speaks the
    beliefs' vocabulary, and the names join the lift lane.
    """
    from app.assistant.entity_management.entity_card_injector import EntityCardInjector
    from app.assistant.agent_runtime.services.entity_injector import EntityInjector
    found = [e for e in (EntityCardInjector().detect_entities_in_text(message) or []) if isinstance(e, str) and e.strip()]
    if not found:
        return message, []
    summaries = EntityInjector().format_entity_cards_leveled(found, level=0)
    return (f"{message}\n{summaries}" if summaries else message), found


def recall(query: str, *, names: list[str], k: int, today: date | None = None) -> list[dict]:
    """Ranked beliefs for the query; each has id, statement, kind, first_day, last_day, score."""
    if not query.strip():
        return []
    from app.assistant.embeddings.embedder import embed_texts
    vec = embed_texts([query])[0]
    picked = rank(beliefs(), vec, names, k, today or datetime.now().date())
    return [{key: b[key] for key in ("id", "statement", "kind", "first_day", "last_day", "score", "relevance")}
            for b in picked]


def format_for_prompt(items: list[dict]) -> str:
    return "\n".join(f"- {b['statement']} (last seen {b['last_day']})" for b in items)


def log_surfaced(items: list[dict], *, room_id: str, message_id: str | None, query: str) -> None:
    """One short transaction per turn; the record everything later is measured against."""
    if not items:
        return
    from belief_engine.intake.store import app_db
    at = datetime.now().isoformat(timespec="seconds")
    with app_db()(True) as c:
        c.execute(_SURFACED_SCHEMA)
        c.executemany(
            "INSERT INTO belief_intake_surfaced (at, room_id, message_id, belief_id, rank, score, query) VALUES (?,?,?,?,?,?,?)",
            [(at, room_id, message_id, b["id"], i, round(b["score"], 4), query) for i, b in enumerate(items, 1)])
