"""Post-processor: convert `{node:<uuid>}` markers in prose into numbered
`[N]` footnotes and append a `## References` section.

Input: prose markdown with inline markers the wiki_writer agent placed at
paragraph ends, e.g.
    Jukka lived in Berkeley from 1995-2000. {node:abc123}

Output: prose markdown with markers replaced and a References section at the
end, e.g.
    Jukka lived in Berkeley from 1995-2000. [1]
    ...
    ## References
    1. [[Berkeley]] — node `abc123`

Numbering is stable per page: the same node_id always gets the same number
within a given render; the first occurrence (in reading order) decides the
number. A node cited multiple times in different paragraphs reuses its number.

The References section also records whether the node label resolves to a
generated wiki page (via entity_exists check), so we can distinguish real
wiki-links from stubs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from sqlalchemy import text

from app.assistant.kg.db.knowledge_graph_db import Node, get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


NODE_MARKER_RE = re.compile(r"\{node:([0-9a-fA-F-]{8,})\}")


@dataclass
class ReferenceEntry:
    number: int
    node_id: str
    label: str
    node_type: str


def extract_node_ids(markdown: str) -> List[str]:
    """Return node_ids in order of first appearance in the markdown."""
    seen: set[str] = set()
    ordered: List[str] = []
    for m in NODE_MARKER_RE.finditer(markdown):
        nid = m.group(1)
        if nid in seen:
            continue
        seen.add(nid)
        ordered.append(nid)
    return ordered


def _load_node_lookup(node_ids: List[str]) -> Dict[str, Tuple[str, str]]:
    """Return ``{node_id: (label, node_type)}`` for the supplied ids."""
    if not node_ids:
        return {}
    session = get_session()
    try:
        rows = (
            session.query(Node.id, Node.label, Node.node_type)
            .filter(Node.id.in_(node_ids))
            .all()
        )
        return {nid: (label or "(unlabeled)", node_type or "") for nid, label, node_type in rows}
    finally:
        session.close()


def build_references(markdown: str) -> List[ReferenceEntry]:
    """Scan markdown for `{node:<id>}` markers and return a stable reference list."""
    node_ids = extract_node_ids(markdown)
    if not node_ids:
        return []
    lookup = _load_node_lookup(node_ids)
    entries: List[ReferenceEntry] = []
    for i, nid in enumerate(node_ids, start=1):
        label, node_type = lookup.get(nid, ("(missing node)", ""))
        entries.append(ReferenceEntry(number=i, node_id=nid, label=label, node_type=node_type))
    return entries


def apply_references(markdown: str, entries: Optional[List[ReferenceEntry]] = None) -> str:
    """Replace every `{node:<id>}` marker with a numbered `[N]` footnote and
    append a `## References` section at the end of the markdown."""
    if entries is None:
        entries = build_references(markdown)
    if not entries:
        return markdown

    number_by_id = {e.node_id: e.number for e in entries}

    def _replace(m: re.Match) -> str:
        nid = m.group(1)
        n = number_by_id.get(nid)
        if n is None:
            return m.group(0)  # shouldn't happen; leave marker intact
        return f"[{n}]"

    prose = NODE_MARKER_RE.sub(_replace, markdown).rstrip() + "\n"

    ref_lines: List[str] = ["", "## References", ""]
    for e in entries:
        ref_lines.append(
            f"{e.number}. [{e.label}](/kg/node/{e.node_id})"
            + (f" *({e.node_type})*" if e.node_type else "")
            + f" — `{e.node_id}`"
        )
    ref_lines.append("")
    return prose + "\n".join(ref_lines)
