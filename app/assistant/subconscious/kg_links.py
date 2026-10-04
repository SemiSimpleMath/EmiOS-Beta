"""The people, places and things a matter names, from the knowledge graph (2026-09-30).

The chat's card-based entity detector (entity_management/entity_card_injector.py) only knows
entities that have cards, so a place like Karjalohja, which is in the graph but has no card, was
invisible to it. The context engine's convergence walk (kg_core/kg_utils/kg_convergence.py) finds
what two entities share but took over an hour a run. Owner, 2026-09-30: search the entity nodes
directly, exactly or by meaning; important entities should still have cards.

For the brain and the brief writer:
- `find_entities(texts)`: every Entity node whose label or alias appears in the texts as a whole
  word or phrase (a trailing possessive allowed, the longest name at each position), the card
  detector's matching rules over all entity nodes. Multi-word names match in any case; a single-word
  name only where the text capitalizes it, unless the whole text is lowercase or the entity is
  central to the graph (CENTRAL_PAGERANK). The primary user's own nodes are left out: every matter is
  the household's, and the owner's node touches most of the graph.
  Each found entity carries its `ties`: the sentences of its links through an Event or State node
  that also links to one of the household's people (the central entities, the owner included), the
  TIES_K most important. An entity named alone otherwise says nothing of whose it is: on 2026-10-02 a
  Woodbridge High announcement became concerns about "the student", the school's own node having no
  description, while "their son's school is Woodbridge High School." sat on its links.
- `shared(entity_ids)`: for two or more entities, what joins them, in one set-based query: the
  Event and State nodes linked to two or more of them (the graph links entities through those,
  not directly), and the other entities reachable from two or more of them through one such node.
  The SHARED_K most important of each.
- `facts_by_meaning(texts)`: the link sentences closest in meaning to each text (the edge collection
  in Chroma, each text embedded whole), the FACTS_K closest overall, read back from SQLite. Names miss
  what a text calls by another name ("WHS", "Woodbridge students"); the meaning finds it. Probe on that
  announcement (2026-10-02, 13,790 sentences): their son's school facts ranked 1st to 5th by the whole
  text; by its subject line alone, 309th and lower.

Code proposes; the brain decides what bears on the matter.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Set, Tuple

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

SHARED_K = 10
TIES_K = 5
FACTS_K = 12
# Entities this central to the graph (PageRank; 39 on 2026-09-30: the household's people, dogs and
# places, plus a few household words such as Dogs and Timesheets) match a lowercase single word in
# any text: the owner often types names in lowercase.
CENTRAL_PAGERANK = 0.005
_MIN_CHARS = 3
_WORD = re.compile(r"[\w'’\-]+", re.UNICODE)


def _norm(token: str) -> str:
    token = token.lower().strip("'’-")
    for suffix in ("'s", "’s"):
        if token.endswith(suffix):
            token = token[: -len(suffix)]
    return token.strip("'’-")


def _tokens(text: str) -> List[str]:
    return [t for t in (_norm(m.group(0)) for m in _WORD.finditer(text or "")) if t]


def _raw(sql: str, params: Iterable[Any] = ()) -> List[tuple]:
    from app.models.db_manager import get_db_manager
    with get_db_manager().read_session() as session:
        return session.connection().connection.driver_connection.execute(sql, tuple(params)).fetchall()


def _owner_ids() -> Set[str]:
    """The primary user's entity nodes: every Entity whose label or alias is the primary user's name."""
    from app.assistant.kg_core.user_identity import get_primary_user_name
    name = get_primary_user_name().strip().lower()
    out = set()
    for node_id, label, aliases in _raw("SELECT id, label, aliases FROM kg_node_metadata WHERE node_type='Entity'"):
        names = [label] + (json.loads(aliases) if aliases and aliases.startswith("[") else [])
        if any(str(n).strip().lower() == name for n in names):
            out.add(node_id)
    return out


def _index() -> Tuple[Dict[tuple, Set[str]], Set[str]]:
    """(normalized token tuple of a label or alias -> entity node ids, the central entities' ids)."""
    index: Dict[tuple, Set[str]] = {}
    central: Set[str] = set()
    for node_id, label, aliases, pagerank in _raw(
            "SELECT id, label, aliases, pagerank_score FROM kg_node_metadata WHERE node_type='Entity'"):
        if (pagerank or 0) >= CENTRAL_PAGERANK:
            central.add(node_id)
        names = [label] + (json.loads(aliases) if aliases and aliases.startswith("[") else [])
        for name in names:
            key = tuple(_tokens(str(name or "")))
            if key and len(" ".join(key)) >= _MIN_CHARS:
                index.setdefault(key, set()).add(node_id)
    return index, central


def _node_views(ids: Iterable[str]) -> List[Dict[str, Any]]:
    ids = list(dict.fromkeys(ids))
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = _raw(f"SELECT id, label, node_type, description, importance, start_date, end_date "
                f"FROM kg_node_metadata WHERE id IN ({marks})", ids)
    by_id = {r[0]: r for r in rows}
    return [{"node_id": i, "label": by_id[i][1], "type": by_id[i][2], "description": by_id[i][3] or "",
             "importance": by_id[i][4], "start": (by_id[i][5] or "")[:10], "end": (by_id[i][6] or "")[:10]}
            for i in ids if i in by_id]


def find_entities(texts: List[str]) -> List[Dict[str, Any]]:
    """Entity nodes named in the texts (label or alias, whole words), the owner's own left out;
    most important first."""
    index, central = _index()
    lengths = sorted({len(k) for k in index}, reverse=True)
    found: Set[str] = set()
    for text in texts:
        words = [m.group(0) for m in _WORD.finditer(text or "")]
        pairs = [(w, _norm(w)) for w in words if _norm(w)]
        toks = [n for _, n in pairs]
        # Case decides a single-word name, except in all-lowercase text (chat typed in lowercase),
        # where it tells nothing: "Water" or "Bed" as entities are noise when the text says water or
        # bed, and names are capitalized where the text uses capitals at all.
        caseless = text == text.lower()
        i = 0
        while i < len(toks):
            # The longest name at each position wins and consumes its words, so "South Lake Middle
            # School" does not also match the generic "Lake" and "School" entities.
            for n in lengths:
                key = tuple(toks[i:i + n])
                if len(key) != n or key not in index:
                    continue
                hits = index[key]
                if n == 1 and not caseless and not pairs[i][0][:1].isupper():
                    hits = hits & central
                    if not hits:
                        continue
                found |= hits
                i += n
                break
            else:
                i += 1
    found -= _owner_ids()
    views = sorted(_node_views(found), key=lambda v: -(v["importance"] or 0))
    ties = household_ties([v["node_id"] for v in views])
    return [{**v, "ties": ties.get(v["node_id"], [])} for v in views]


def _household_ids() -> List[str]:
    """The household's people and places: the central entities, the owner's own nodes included."""
    central = [r[0] for r in _raw("SELECT id FROM kg_node_metadata WHERE node_type='Entity' AND pagerank_score >= ?",
                                  (CENTRAL_PAGERANK,))]
    return list(dict.fromkeys(central + sorted(_owner_ids())))


def household_ties(entity_ids: List[str]) -> Dict[str, List[str]]:
    """entity id -> the sentences of its links through an Event or State node that also links to one of the
    household's people, the TIES_K most important each. A household entity is not tied to itself."""
    ids = list(dict.fromkeys(entity_ids))
    household = _household_ids()
    if not ids or not household:
        return {}
    marks, hmarks = ",".join("?" * len(ids)), ",".join("?" * len(household))
    rows = _raw(f"""
        WITH e(a, b, sentence, importance) AS (
            SELECT source_id, target_id, sentence, importance FROM kg_edge_metadata
            UNION ALL SELECT target_id, source_id, sentence, importance FROM kg_edge_metadata)
        SELECT DISTINCT x.a, x.sentence, x.importance FROM e x
        JOIN kg_node_metadata m ON m.id = x.b AND m.node_type IN ('Event', 'State')
        JOIN e y ON y.a = x.b AND y.b IN ({hmarks}) AND y.b != x.a
        WHERE x.a IN ({marks}) AND x.sentence IS NOT NULL AND x.sentence != ''
        ORDER BY x.importance DESC""", [*household, *ids])
    out: Dict[str, List[str]] = {}
    for entity_id, sentence, _ in rows:
        kept = out.setdefault(entity_id, [])
        if sentence not in kept and len(kept) < TIES_K:
            kept.append(sentence)
    return out


def facts_by_meaning(texts: List[str]) -> List[Dict[str, Any]]:
    """The FACTS_K link sentences closest in meaning to any of the texts, closest first: {sentence, score}.
    Each text is embedded whole; the edge index answers by id and the sentences are read from SQLite."""
    from app.assistant.embeddings.embedder import embed_texts
    from app.assistant.kg.chroma.chroma_embedding_manager import get_chroma_manager
    queries = [t for t in texts if str(t or "").strip()]
    if not queries:
        return []
    manager = get_chroma_manager()
    best: Dict[str, float] = {}
    for vector in embed_texts(queries):
        for edge_id, score in manager.search_similar_edges(vector, k=FACTS_K):
            best[edge_id] = max(score, best.get(edge_id, 0.0))
    if not best:
        return []
    marks = ",".join("?" * len(best))
    sentences = dict(_raw(f"SELECT id, sentence FROM kg_edge_metadata WHERE id IN ({marks})", list(best)))
    out, seen = [], set()
    for edge_id in sorted(best, key=lambda i: -best[i]):
        sentence = sentences.get(edge_id)
        if sentence and sentence not in seen:
            seen.add(sentence)
            out.append({"sentence": sentence, "score": round(best[edge_id], 3)})
    return out[:FACTS_K]


def shared(entity_ids: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """What joins two or more entities: the Event/State nodes linked to two or more of them, and the
    other entities reachable from two or more of them through one Event/State node."""
    ids = list(dict.fromkeys(entity_ids))
    if len(ids) < 2:
        return {"links": [], "entities": []}
    marks = ",".join("?" * len(ids))
    owner = list(_owner_ids()) or [""]
    omarks = ",".join("?" * len(owner))
    link_rows = _raw(f"""
        WITH e(a, b) AS (SELECT source_id, target_id FROM kg_edge_metadata
                         UNION ALL SELECT target_id, source_id FROM kg_edge_metadata)
        SELECT e.b, COUNT(DISTINCT e.a) AS k FROM e JOIN kg_node_metadata n ON n.id = e.b
        WHERE e.a IN ({marks}) AND n.node_type IN ('Event', 'State')
        GROUP BY e.b HAVING k >= 2 ORDER BY k DESC, n.importance DESC LIMIT ?""", [*ids, SHARED_K])
    entity_rows = _raw(f"""
        WITH e(a, b) AS (SELECT source_id, target_id FROM kg_edge_metadata
                         UNION ALL SELECT target_id, source_id FROM kg_edge_metadata),
        via AS (SELECT e.a AS seed, e.b AS mid FROM e JOIN kg_node_metadata m ON m.id = e.b
                WHERE e.a IN ({marks}) AND m.node_type IN ('Event', 'State')),
        far AS (SELECT via.seed, e.b AS node FROM via JOIN e ON e.a = via.mid)
        SELECT far.node, COUNT(DISTINCT far.seed) AS k FROM far JOIN kg_node_metadata n ON n.id = far.node
        WHERE n.node_type = 'Entity' AND far.node NOT IN ({marks}) AND far.node NOT IN ({omarks})
        GROUP BY far.node HAVING k >= 2 ORDER BY k DESC, n.importance DESC LIMIT ?""",
                       [*ids, *ids, *owner, SHARED_K])
    return {"links": _node_views(r[0] for r in link_rows), "entities": _node_views(r[0] for r in entity_rows)}
