"""
Split extractor output (nodes + edges for a window) into connected
subgraphs. Each connected subgraph becomes one claim_proposal group.

Rationale (memory: project_kg_proposals_redesign_pending):
  A window can yield a single coherent structure OR two disconnected
  structures covering unrelated topics. Those are distinct claims and
  must become separate proposals so they can be reviewed, promoted, or
  retracted independently.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple


Node = Dict[str, Any]
Edge = Dict[str, Any]


def split_into_connected_components(
    nodes: Iterable[Node],
    edges: Iterable[Edge],
    *,
    source_key: str = "source",
    target_key: str = "target",
    node_key: str = "temp_id",
) -> List[Tuple[List[Node], List[Edge]]]:
    """Return list of (component_nodes, component_edges) pairs.

    Nodes are connected if any edge has one as source and the other as
    target (edges treated as undirected for component detection only;
    edge direction is preserved within the returned edge list).

    Isolated nodes (no incident edges) form their own single-node
    component. Edges whose source or target doesn't appear in ``nodes``
    are preserved but may create orphan components — this is a signal
    of bad extractor output and worth logging upstream.
    """
    nodes = list(nodes)
    edges = list(edges)

    # temp_id -> node dict (preserves insertion order of nodes)
    nodes_by_id: Dict[str, Node] = {}
    for n in nodes:
        tid = n.get(node_key)
        if tid is None:
            continue
        nodes_by_id[str(tid)] = n

    # Union-Find over temp_ids encountered in nodes or edges.
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for tid in nodes_by_id:
        parent[tid] = tid

    for e in edges:
        s = e.get(source_key)
        t = e.get(target_key)
        if s is None or t is None:
            continue
        s, t = str(s), str(t)
        # Also add endpoints as roots if they weren't in the nodes list
        # (belt and suspenders; really should never happen).
        for endpoint in (s, t):
            if endpoint not in parent:
                parent[endpoint] = endpoint
        union(s, t)

    # Group nodes by root.
    groups: Dict[str, Dict[str, Any]] = {}
    for tid, node in nodes_by_id.items():
        root = find(tid)
        grp = groups.setdefault(root, {"nodes": [], "edges": []})
        grp["nodes"].append(node)

    # Group edges by root (picks source's root).
    for e in edges:
        s = e.get(source_key)
        if s is None:
            continue
        root = find(str(s))
        grp = groups.setdefault(root, {"nodes": [], "edges": []})
        grp["edges"].append(e)

    # Deterministic ordering: first by first-node-appearance in original list.
    order: Dict[str, int] = {}
    for idx, n in enumerate(nodes):
        tid = n.get(node_key)
        if tid is None:
            continue
        root = find(str(tid))
        if root not in order:
            order[root] = idx

    result: List[Tuple[List[Node], List[Edge]]] = []
    for root in sorted(groups.keys(), key=lambda r: order.get(r, 1_000_000)):
        grp = groups[root]
        result.append((grp["nodes"], grp["edges"]))
    return result
