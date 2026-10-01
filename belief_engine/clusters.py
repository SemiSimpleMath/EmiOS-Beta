"""Beliefs grouped into named topics (2026-09-30).

Owner, 2026-09-30: "Really relevant beliefs should cluster. If you read the beliefs in that cluster we
would not have picked it as relevant for today." The routine's belief selector had picked "completes
timesheets weekly … aims to finish by 3–4 PM" for a Wednesday without seeing "weekly timesheets are due
on Mondays", which sat on another page of its catalog. A topic keeps a belief with the beliefs that
qualify it (its day, its limit, its exception, the owner's reminder wishes), so every reader sees them
together: the selector judges a topic as a whole, the writer gets a selected belief's topic-mates.

Code proposes, the model decides:
- the proposal groups the beliefs to place by closeness of meaning (average-linkage over their
  embeddings, cosine similarity PROPOSAL_SIMILARITY);
- `belief_engine::belief_clusterer` places every belief exactly once, into an existing topic (its label
  copied) or a new one, splitting and merging the proposal as the meaning requires;
- code keeps every belief the answer placed exactly once (two entries with the same label are one
  topic) and asks once more for the rest, the beliefs missed or placed twice; a belief still unplaced
  after that fails the run.

`place_beliefs` places the active beliefs that belong to no topic yet: all of them the first time, then
the new ones (nightly, belief_tag_v1). The beliefs to place go in pages; each page's topics are
existing topics for the next. A retired belief keeps its membership; readers take active beliefs only.

Tables in emi.db: `belief_clusters` (id K<n>, label) and `belief_cluster_members` (belief_id, cluster_id).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_AGENT = "belief_engine::belief_clusterer"
PROPOSAL_SIMILARITY = 0.72
_PAGE_CHARS = 8000           # beliefs to place per call (about 70), bounded by size, never cut: at 24000
                             # (about 200) the model missed or doubled a belief (2026-09-30)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS belief_clusters (
        id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS belief_cluster_members (
        belief_id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL REFERENCES belief_clusters(id),
        assigned_at TEXT NOT NULL)""",
]


def _connect():
    from belief_engine.intake.store import app_db
    return app_db()


def ensure_schema(connect=None) -> None:
    connect = connect or _connect()
    with connect(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def memberships(connect=None) -> Dict[str, Dict[str, str]]:
    """belief_id -> {cluster_id, cluster}."""
    connect = connect or _connect()
    ensure_schema(connect)
    with connect(False) as c:
        return {r[0]: {"cluster_id": r[1], "cluster": r[2]} for r in c.execute(
            "SELECT m.belief_id, k.id, k.label FROM belief_cluster_members m "
            "JOIN belief_clusters k ON k.id = m.cluster_id")}


def _active_beliefs(connect) -> List[Dict[str, Any]]:
    with connect(False) as c:
        return [{"id": r[0], "statement": r[1], "embedding": json.loads(r[2])} for r in c.execute(
            "SELECT id, statement, embedding FROM belief_intake_beliefs WHERE status='active' "
            "ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)")]


def propose_groups(beliefs: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Beliefs grouped by closeness of meaning: average linkage over cosine distance, cut at
    PROPOSAL_SIMILARITY. Groups in order of their first belief."""
    if len(beliefs) < 2:
        return [list(beliefs)] if beliefs else []
    import numpy as np
    from scipy.cluster.hierarchy import fcluster, linkage
    vectors = np.array([b["embedding"] for b in beliefs], dtype=float)
    labels = fcluster(linkage(vectors, method="average", metric="cosine"),
                      t=1 - PROPOSAL_SIMILARITY, criterion="distance")
    groups: Dict[int, List[Dict[str, Any]]] = {}
    for belief, label in zip(beliefs, labels):
        groups.setdefault(int(label), []).append(belief)
    return list(groups.values())


def _pages(groups: List[List[Dict[str, Any]]], max_chars: int) -> List[List[List[Dict[str, Any]]]]:
    """Whole proposed groups per page, bounded by statement size."""
    pages, page, size = [], [], 0
    for g in groups:
        length = sum(len(b["statement"]) + 12 for b in g)
        if page and size + length > max_chars:
            pages.append(page)
            page, size = [], 0
        page.append(g)
        size += length
    if page:
        pages.append(page)
    return pages


def _settle(data: Any, wanted: List[str]) -> tuple[List[Dict[str, Any]], List[str], List[str]]:
    """(topics with every belief placed exactly once, grouped by label; the beliefs left to place;
    what was wrong). Entries sharing a label, in any case, are one topic. An id not given is ignored."""
    if not isinstance(data, dict) or not isinstance(data.get("clusters"), list):
        return [], list(wanted), ["the answer had no clusters list"]
    placements = [(str((c or {}).get("label") or "").strip(), bid)
                  for c in data["clusters"] for bid in (c or {}).get("belief_ids") or []]
    notes = []
    unknown = sorted({bid for _, bid in placements if bid not in wanted})
    if unknown:
        notes.append(f"{unknown} are not beliefs to place")
    counts: Dict[str, int] = {}
    for label, bid in placements:
        if bid in wanted:
            counts[bid] = counts.get(bid, 0) + 1
    topics: Dict[str, Dict[str, Any]] = {}
    for label, bid in placements:
        if bid in wanted and counts[bid] == 1 and label:
            topics.setdefault(label.lower(), {"label": label, "belief_ids": []})["belief_ids"].append(bid)
    twice = sorted(b for b, n in counts.items() if n > 1)
    if twice:
        notes.append(f"placed more than once: {twice}")
    unlabelled = sorted(bid for label, bid in placements if bid in wanted and counts[bid] == 1 and not label)
    if unlabelled:
        notes.append(f"placed in a cluster with no label: {unlabelled}")
    placed = {bid for t in topics.values() for bid in t["belief_ids"]}
    left = [b for b in wanted if b not in placed]
    missing = [b for b in left if b not in counts]
    if missing:
        notes.append(f"not placed: {missing}")
    return list(topics.values()), left, notes


def _agent_call(payload: Dict[str, Any]) -> Any:
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    from belief_engine.intake.agents import scope
    agent = DI.agent_factory.create_agent(_AGENT)
    if agent is None:
        raise RuntimeError(f"agent {_AGENT!r} not found")
    return getattr(agent.action_handler(Message(agent_input=payload, scope_context=scope())), "data", None)


def _existing_view(connect, active_ids: set) -> List[Dict[str, Any]]:
    with connect(False) as c:
        rows = c.execute("SELECT k.label, b.id, b.statement FROM belief_clusters k "
                         "JOIN belief_cluster_members m ON m.cluster_id = k.id "
                         "JOIN belief_intake_beliefs b ON b.id = m.belief_id "
                         "ORDER BY k.label, CAST(SUBSTR(b.id, 2) AS INTEGER)").fetchall()
    view: Dict[str, List[str]] = {}
    for label, bid, statement in rows:
        if bid in active_ids:
            view.setdefault(label, []).append(statement)
    return [{"label": label, "beliefs": statements} for label, statements in view.items()]


def _write(connect, clusters: List[Dict[str, Any]]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    created = 0
    with connect(True) as c:
        by_label = {r[1].lower(): r[0] for r in c.execute("SELECT id, label FROM belief_clusters")}
        top = max([int(k[1:]) for k in by_label.values()] or [0])
        for cluster in clusters:
            label = cluster["label"].strip()
            cid = by_label.get(label.lower())
            if cid is None:
                top += 1
                cid = f"K{top}"
                c.execute("INSERT INTO belief_clusters (id, label, created_at) VALUES (?, ?, ?)", (cid, label, now))
                by_label[label.lower()] = cid
                created += 1
            c.executemany("INSERT OR REPLACE INTO belief_cluster_members (belief_id, cluster_id, assigned_at) "
                          "VALUES (?, ?, ?)", [(bid, cid, now) for bid in cluster["belief_ids"]])
    return created


def place_beliefs(*, connect=None, call: Optional[Callable[[Dict[str, Any]], Any]] = None) -> Dict[str, int]:
    """Place every active belief that belongs to no topic. Free when there is none. Raises when a page's
    answer is still invalid after one correction; the pages before it stay written."""
    connect = connect or _connect()
    call = call or _agent_call
    ensure_schema(connect)
    active = _active_beliefs(connect)
    placed = set(memberships(connect))
    todo = [b for b in active if b["id"] not in placed]
    if not todo:
        return {"placed": 0, "clusters_created": 0}
    active_ids = {b["id"] for b in active}
    created = 0
    statement = {b["id"]: b["statement"] for b in todo}
    for page in _pages(propose_groups(todo), _PAGE_CHARS):
        wanted = [b["id"] for g in page for b in g]
        topics, left, notes = _settle(call({
            "existing_clusters": _existing_view(connect, active_ids),
            "beliefs_to_place": [[{"id": b["id"], "statement": b["statement"]} for b in g] for g in page],
            "correction": ""}), wanted)
        created += _write(connect, topics)
        if left:
            logger.warning("[clusters] %d belief(s) to place again: %s", len(left), notes)
            topics, still, notes = _settle(call({
                "existing_clusters": _existing_view(connect, active_ids),
                "beliefs_to_place": [[{"id": bid, "statement": statement[bid]} for bid in left]],
                "correction": "; ".join(notes)}), left)
            created += _write(connect, topics)
            if still:
                raise ValueError(f"belief clustering: {still} still unplaced after one correction: {notes}")
    logger.info("[clusters] placed %d belief(s); %d new topic(s)", len(todo), created)
    return {"placed": len(todo), "clusters_created": created}
