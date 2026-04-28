from typing import Any, Dict, List, Tuple


def chunk_sentences(sentences: List[str], size: int = 5) -> List[List[str]]:
    """
    Ported behavior from legacy KG pipeline:
    process atomic sentences in chunks of up to 5.
    """
    if not sentences:
        return []
    size = max(1, int(size))
    return [sentences[i : i + size] for i in range(0, len(sentences), size)]


def normalize_extraction_result(extraction_data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Normalize fact_extractor output into plain node/edge dicts.

    Legacy compatibility:
    - map node['core'] -> node['category'] when present
    """
    nodes = extraction_data.get("nodes", []) if isinstance(extraction_data, dict) else []
    edges = extraction_data.get("edges", []) if isinstance(extraction_data, dict) else []

    normalized_nodes: List[Dict[str, Any]] = []
    for n in nodes:
        if isinstance(n, dict):
            node = dict(n)
        else:
            node = {
                "node_type": getattr(n, "node_type", None),
                "temp_id": getattr(n, "temp_id", None),
                "label": getattr(n, "label", None),
                "core": getattr(n, "core", None),
                "sentence": getattr(n, "sentence", None),
            }
        if "core" in node and "category" not in node:
            node["category"] = node.get("core")
        normalized_nodes.append(node)

    normalized_edges: List[Dict[str, Any]] = []
    for e in edges:
        if isinstance(e, dict):
            edge = dict(e)
        else:
            edge = {
                "temp_id": getattr(e, "temp_id", None),
                "relationship_type": getattr(e, "relationship_type", None),
                "label": getattr(e, "label", None),
                "source": getattr(e, "source", None),
                "target": getattr(e, "target", None),
                "bidirectional": getattr(e, "bidirectional", False),
                "sentence": getattr(e, "sentence", None),
            }
        normalized_edges.append(edge)

    return normalized_nodes, normalized_edges

