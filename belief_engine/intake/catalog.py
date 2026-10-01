"""The active belief catalog, read from the intake's store for every consumer.

One reader for the export (routine writer, health, entertainment, feedback-extractor context,
insights page) and for beliefs_for_context (the meal lane). Each entry has the shape the legacy
export had, so consumers keep their fields:

    belief_key / short_id  the intake id ("B534") — stable, never reused, what routines cite and
                           work objects carry in belief_refs
    statement, kind, scope, status
    tags                   from belief_tags (the shared retrieval vocabulary)
    domain                 the first tag, for displays that group by it; "" when untagged
    observation_count      supporting evidence rows
    first_observed / last_confirmed   first / last day with supporting evidence
    refines                the parent belief's id, for a one-level refinement
    cluster_id / cluster   the topic the belief belongs to and its label (belief_engine/clusters.py);
                           None until the belief is placed

There is no confidence band: an intake belief is a view of its evidence, and the evidence counts
and dates above are what a consumer can weigh.
"""
from __future__ import annotations

from typing import Any, Dict, List

_P = "belief_intake_"


def active_entries(*, conn=None) -> List[Dict[str, Any]]:
    """Every active intake belief as a consumer entry, in id order."""
    from belief_engine import clusters
    if conn is not None:
        for statement in clusters.SCHEMA:     # the topic tables, on the connection the caller holds
            conn.execute(statement)
        return _entries(conn)
    from belief_engine.intake.store import app_db
    clusters.ensure_schema()
    with app_db()(False) as c:
        return _entries(c)


def _entries(c) -> List[Dict[str, Any]]:
    rows = c.execute(f"SELECT id, statement, kind, scope, status, parent_id, created_day FROM {_P}beliefs "
                     "WHERE status='active' ORDER BY CAST(SUBSTR(id, 2) AS INTEGER)").fetchall()
    support = {r[0]: (r[1], r[2], r[3]) for r in c.execute(
        f"SELECT belief_id, MIN(day), MAX(day), COUNT(*) FROM {_P}evidence WHERE relation='support' GROUP BY belief_id")}
    topic = {r[0]: (r[1], r[2]) for r in c.execute(
        "SELECT m.belief_id, k.id, k.label FROM belief_cluster_members m JOIN belief_clusters k ON k.id = m.cluster_id")}
    tags: Dict[str, List[str]] = {}
    for bid, tag in c.execute("SELECT belief_id, tag FROM belief_tags WHERE belief_id GLOB 'B[0-9]*' ORDER BY tag"):
        tags.setdefault(bid, []).append(tag)
    out = []
    for r in rows:
        bid = r[0]
        first, last, n = support.get(bid, (r[6], r[6], 0))
        t = tags.get(bid, [])
        out.append({
            "belief_key": bid, "short_id": bid, "statement": r[1], "kind": r[2], "scope": r[3],
            "status": r[4], "tags": t, "domain": t[0] if t else "", "confidence": None,
            "observation_count": n, "first_observed": first, "last_confirmed": last,
            "refines": r[5],
            "cluster_id": topic.get(bid, (None, None))[0], "cluster": topic.get(bid, (None, None))[1],
        })
    return out
