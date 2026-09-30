"""What the brain can see of the household's past and ongoing work (2026-09-30, step 4).

A matter is rarely new. An email about the bake sale may follow work done on last year's bake sale;
a changed date may break a reminder, a calendar entry or an arrangement already made. The brain step
gets, for each matter:

- linked work, found exactly by code:
    work that cites one of the matter's concerns (constraints.concern_refs), and
    work created from an email in one of the matter's Gmail threads (constraints.source_intake);
- similar past work: the nearest work objects by meaning (a vector index over each work object's
  title and objective, local MiniLM embedder), for what was never linked. Code proposes; the brain
  decides what is the same matter. Work already linked exactly is not repeated;
- what is already in motion: every active work object and every live scheduled reminder, so the
  brain can name what depends on a fact that changed. (The calendar is read by brain_step once per
  run.)

Past work is shown as title, status, when, why it ended and each task's finalizer outcome: the same
summary dayflow's steward reads (shared/work/finalizer_summary.j2).

The index lives in `brain_work_index` (emi.db): one row per work object with the `updated_at` it was
embedded at; `refresh_index` embeds only what is new or changed since.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

SIMILAR_K = 5
# Cosine similarity (all-MiniLM-L6-v2) below which a past work object is not proposed. Calibrated
# on the live store (1,513 work objects) 2026-09-30: true matches scored 0.74-0.84 (flea medication,
# OpenAI charges), 0.53-0.58 (library fine) and 0.38 ("haven't booked my physical" -> the active
# notify-to-schedule work); the best unrelated hits scored 0.33-0.35 and an unrelated chat line 0.22.
# The brain decides what is the same matter, so a near miss costs a few lines, a miss costs history.
MIN_SIMILARITY = 0.36

SCHEMA = """CREATE TABLE IF NOT EXISTS brain_work_index (
    work_id TEXT PRIMARY KEY,
    updated_at TEXT NOT NULL,
    text TEXT NOT NULL,
    vector BLOB NOT NULL)"""


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        c.execute(SCHEMA)


def _store():
    from app.assistant.dayflow_orchestrator.work_store import get_dayflow_work_store
    return get_dayflow_work_store()


def _rows() -> List[Dict[str, Any]]:
    """Every work object's id, title, status, times and constraints. Read on the store's own
    connection under its lock, as search_work_objects does: loading 1,500 graphs to read one
    column each would parse every node."""
    store = _store()
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, title, status, created_at, updated_at, constraints FROM work_objects").fetchall()
    return [{"id": r[0], "title": r[1] or "", "status": r[2], "created_at": r[3], "updated_at": r[4],
             "constraints": json.loads(r[5] or "{}")} for r in rows]


def _local(value: Any) -> str:
    from app.assistant.utils.time_utils import parse_iso_utc, utc_to_local
    when = parse_iso_utc(str(value or ""))
    return utc_to_local(when).strftime("%a %Y-%m-%d") if when else ""


def work_view(work_id: str, *, link: str, score: Optional[float] = None) -> Dict[str, Any]:
    """One work object as the brain reads it: what it was for, how it ended, what each task found."""
    from app.assistant.control_nodes.strategic_planner_wo_prep_node import _goal_epitaph
    wo = _store().load(work_id)
    nodes = getattr(wo, "nodes", {}) or {}
    store = _store()
    with store._lock:
        row = store._conn.execute("SELECT title, status, created_at, updated_at FROM work_objects WHERE id=?",
                                  (work_id,)).fetchone()
    summary = {"title": row[0] or "", "status": row[1], "created_at": row[2], "updated_at": row[3]}
    return {
        "work_id": work_id, "title": wo.title or summary["title"], "status": summary["status"],
        "objective": str((wo.constraints or {}).get("objective") or ""),
        "created": _local(summary["created_at"]), "updated": _local(summary["updated_at"]),
        "ended_because": _goal_epitaph(wo),
        "outcomes": [n.payload["finalizer"] for n in nodes.values()
                     if wo.is_work_unit(n) and (n.payload or {}).get("finalizer")],
        "link": link, "score": round(score, 2) if score is not None else None,
    }


def _cites(refs: Iterable[str], concern_id: str) -> bool:
    for ref in refs:
        ref = str(ref or "").strip()
        ref = ref[len("concern:"):] if ref.startswith("concern:") else ref
        if ref and (ref == concern_id or (len(ref) >= 8 and concern_id.startswith(ref))):
            return True
    return False


def linked_work(concern_ids: List[str], threads: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Work citing one of the concerns, or created from an email in one of the threads
    ({account_id, thread_id}). Newest first."""
    thread_keys = {(t["account_id"], t["thread_id"]) for t in threads if t.get("thread_id")}
    found: Dict[str, str] = {}
    for row in sorted(_rows(), key=lambda r: r["updated_at"] or "", reverse=True):
        refs = row["constraints"].get("concern_refs") or []
        cited = [cid for cid in concern_ids if _cites(refs, cid)]
        if cited:
            found.setdefault(row["id"], "cites a concern of this matter")
        for s in row["constraints"].get("source_intake") or []:
            item = str(s.get("item_id") or "")
            account = item.split(":")[1] if item.startswith("dayflow_email:") and item.count(":") >= 2 else ""
            if (account, str(s.get("thread_id") or "")) in thread_keys:
                found.setdefault(row["id"], "created from an email in this thread")
    return [work_view(wid, link=why) for wid, why in found.items()]


# ── the similarity index ────────────────────────────────────────────────────

def _index_text(row: Dict[str, Any]) -> str:
    objective = str(row["constraints"].get("objective") or "").strip()
    return row["title"] if not objective or objective.startswith(row["title"]) else f"{row['title']}. {objective}"


def _embed(texts: List[str]) -> List[List[float]]:
    from app.assistant.embeddings.embedder import embed_texts
    return embed_texts(texts)


def refresh_index(connect=None) -> int:
    """Embed every work object that is new or changed since it was indexed. Returns how many."""
    import numpy as np
    connect = connect or _connect
    ensure_schema(connect)
    with connect(False) as c:
        indexed = {r[0]: r[1] for r in c.execute("SELECT work_id, updated_at FROM brain_work_index")}
    stale = [r for r in _rows() if indexed.get(r["id"]) != (r["updated_at"] or "")]
    if not stale:
        return 0
    texts = [_index_text(r) for r in stale]
    vectors = _embed(texts)
    with connect(True) as c:
        c.executemany(
            "INSERT INTO brain_work_index (work_id, updated_at, text, vector) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(work_id) DO UPDATE SET updated_at=excluded.updated_at, text=excluded.text, "
            "vector=excluded.vector",
            [(r["id"], r["updated_at"] or "", t, np.asarray(v, dtype=np.float32).tobytes())
             for r, t, v in zip(stale, texts, vectors)])
    logger.info("[work_links] indexed %d work object(s)", len(stale))
    return len(stale)


def similar_work(texts: List[str], *, exclude: Iterable[str] = (), k: int = SIMILAR_K,
                 min_similarity: float = MIN_SIMILARITY, connect=None) -> List[Dict[str, Any]]:
    """The k past work objects nearest to any of `texts` (cosine, best over the texts), at or above
    `min_similarity`, excluding `exclude`. Refreshes the index first."""
    import numpy as np
    connect = connect or _connect
    texts = [t for t in texts if str(t or "").strip()]
    if not texts:
        return []
    refresh_index(connect)
    with connect(False) as c:
        rows = c.execute("SELECT work_id, vector FROM brain_work_index").fetchall()
    skip = set(exclude)
    rows = [r for r in rows if r[0] not in skip]
    if not rows:
        return []
    matrix = np.stack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    matrix = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
    queries = np.asarray(_embed(texts), dtype=np.float32)
    queries = queries / np.linalg.norm(queries, axis=1, keepdims=True)
    scores = (queries @ matrix.T).max(axis=0)
    best = [i for i in np.argsort(-scores)[:k] if scores[i] >= min_similarity]
    return [work_view(rows[i][0], link="similar", score=float(scores[i])) for i in best]


# ── what is already in motion ───────────────────────────────────────────────

def active_work() -> List[Dict[str, Any]]:
    """Every active work object: title, objective, when it started."""
    return [{"work_id": r["id"], "title": r["title"], "objective": str(r["constraints"].get("objective") or ""),
             "created": _local(r["created_at"])}
            for r in sorted(_rows(), key=lambda r: r["created_at"] or "") if r["status"] == "active"]


def _repeats(seconds: Optional[int]) -> str:
    if not seconds:
        return ""
    for unit, size in (("year", 31557600), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        # A year is 365.25 days, so a yearly interval is matched by size, the rest exactly.
        if seconds >= size and (unit == "year" or seconds % size == 0):
            n = round(seconds / size)
            return f"every {unit}" if n == 1 else f"every {n} {unit}s"
    return f"every {seconds} seconds"


def live(rows: List[Any], now: datetime) -> List[Dict[str, Any]]:
    """Scheduled reminders (time_events rows) that can still fire: recurring ones not yet ended,
    one-time ones not yet due. Title, kind, how often, start, end; local times."""
    from app.assistant.utils.time_utils import utc_to_local

    def parse(value):
        if not value:
            return None
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    out = []
    for event_type, interval, start, end, payload in rows:
        start_dt, end_dt = parse(start), parse(end)
        if event_type == "one_time_event" and start_dt is not None and start_dt < now:
            continue
        if end_dt is not None and end_dt < now:
            continue
        p = json.loads(payload or "{}")
        out.append({"title": str(p.get("event_title") or p.get("title") or p.get("message") or ""),
                    "kind": "one time" if event_type == "one_time_event" else "recurring",
                    "repeats": _repeats(interval),
                    "start": utc_to_local(start_dt).strftime("%a %Y-%m-%d %H:%M") if start_dt else "",
                    "end": utc_to_local(end_dt).strftime("%a %Y-%m-%d %H:%M") if end_dt else ""})
    return sorted(out, key=lambda r: r["title"])


def live_reminders(now_utc: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Every scheduled reminder that can still fire (the scheduler's time_events table)."""
    from app.models.db_manager import get_db_manager
    with get_db_manager().read_session() as session:
        raw = session.connection().connection.driver_connection
        rows = raw.execute("SELECT event_type, interval, start_date, end_date, event_payload FROM time_events").fetchall()
    return live(rows, now_utc or datetime.now(timezone.utc))
