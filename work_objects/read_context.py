"""Read capability bound to a work invocation, never to model-supplied work IDs."""
READ_TOOLS = frozenset({"work_graph_summary", "work_graph_search", "work_graph_peek", "work_artifact_fetch"})

def active_read_tools():
    from work_objects.runtime import peek_work_context
    return READ_TOOLS if peek_work_context() is not None else frozenset()

def node_header(node):
    return {"id": node.id, "title": node.title or f"{node.type} {node.id}",
            "type": node.type, "status": node.status, "parent_id": node.parent_id}

def artifact_index(wo, node_id):
    """Owned outputs and explicitly produced outputs, without bodies."""
    records = {n.id: n for n in wo.provenance_for(node_id) if n.type not in {"subtask", "goal"}}
    for edge in wo.edges:
        if edge.src == node_id and edge.relation == "produces" and edge.dst in wo.nodes:
            records[edge.dst] = wo.nodes[edge.dst]
    return [{**node_header(n), "pod_ref": n.pod_ref} for n in records.values()]
