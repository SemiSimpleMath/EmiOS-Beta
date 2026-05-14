import logging
from flask import Blueprint, jsonify, request
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge, NODE_TYPES
from app.models.base import get_session
from datetime import datetime, timezone
from sqlalchemy import distinct, func, or_

from app.assistant.utils.time_utils import to_rfc3339_z, utc_now

logger = logging.getLogger(__name__)

graph_api = Blueprint('graph_api', __name__)


def _node_degree(session, node_id: str) -> int:
    """Count of edges touching ``node_id`` — server-side truth for a node's
    degree. Used so every API response includes a concrete degree regardless
    of which endpoint returned the node."""
    return session.query(func.count(Edge.id)).filter(
        or_(Edge.source_id == node_id, Edge.target_id == node_id)
    ).scalar() or 0


def _degree_map_for(session, node_ids) -> dict:
    """Bulk degree lookup — returns ``{node_id: degree}`` for the given ids.
    Uses two GROUP BY queries (one for source_id, one for target_id) over
    the ORM; SQLAlchemy handles the IN-list expansion correctly across
    dialects. Avoids firing N single-node degree queries when a response
    returns a list of nodes."""
    ids = list(node_ids)
    if not ids:
        return {}
    out: dict = {}
    src_rows = (
        session.query(Edge.source_id, func.count(Edge.id))
        .filter(Edge.source_id.in_(ids))
        .group_by(Edge.source_id)
        .all()
    )
    for nid, n in src_rows:
        out[nid] = out.get(nid, 0) + int(n or 0)
    tgt_rows = (
        session.query(Edge.target_id, func.count(Edge.id))
        .filter(Edge.target_id.in_(ids))
        .group_by(Edge.target_id)
        .all()
    )
    for nid, n in tgt_rows:
        out[nid] = out.get(nid, 0) + int(n or 0)
    return out


def _serialize_pod_uri_as_node(pod_uri: str, *, degree: int | None = None) -> dict | None:
    """Render a pod URI as a node-shaped dict for the visualizer.

    Pods don't have kg_node_metadata rows (no mirror — pod_store is the sole
    source of truth). When a BFS hits a pod URI as an edge endpoint, this
    fetches metadata from pod_store and returns the same shape as
    _serialize_node so the frontend renders it without special-casing.

    Returns None if the URI isn't a valid pod URI or the pod is missing
    from pod_store (genuine dangling edge).
    """
    from app.assistant.pod_store.pod_uri import POD_URI_RE
    if not POD_URI_RE.fullmatch(pod_uri or ""):
        return None
    from app.assistant.pod_store.pod_store import PodStore
    pod = PodStore().get(pod_uri)
    if pod is None:
        return None
    return {
        'id': pod_uri,
        'label': (pod.one_liner or pod_uri)[:200],
        'type': 'Pod',
        'category': pod.kind,
        'aliases': [],
        'description': pod.one_liner,
        'original_sentence': (pod.body or "")[:400] if pod.body else None,
        'attributes': {},
        'start_date': None,
        'end_date': None,
        'start_date_confidence': None,
        'end_date_confidence': None,
        'created_at': pod.created_at.isoformat() if getattr(pod, 'created_at', None) else None,
        'updated_at': None,
        'valid_during': None,
        'hash_tags': list(pod.tags or []),
        'semantic_label': None,
        'goal_status': None,
        'confidence': None,
        'importance': None,
        'source': 'pod_store',
        'degree': degree,
        'taxonomy_paths': [],
    }


def _serialize_node(node: Node, *, degree: int | None = None) -> dict:
    return {
        'id': str(node.id),
        'label': node.label,
        'type': node.node_type,
        'category': node.category,
        'aliases': node.aliases if node.aliases else [],
        'description': node.description,
        'original_sentence': node.original_sentence,
        'attributes': node.attributes if node.attributes else {},
        'start_date': node.start_date.isoformat() if node.start_date else None,
        'end_date': node.end_date.isoformat() if node.end_date else None,
        'start_date_confidence': node.start_date_confidence,
        'end_date_confidence': node.end_date_confidence,
        'created_at': node.created_at.isoformat() if node.created_at else None,
        'updated_at': node.updated_at.isoformat() if node.updated_at else None,
        'valid_during': node.valid_during,
        'hash_tags': node.hash_tags if node.hash_tags else [],
        'semantic_label': node.semantic_label,
        'goal_status': node.goal_status,
        'confidence': node.confidence,
        'importance': node.importance,
        'source': node.source,
        'degree': degree,
        'taxonomy_paths': [],
    }


def _serialize_edge(edge: Edge) -> dict:
    return {
        'id': str(edge.id),
        'source_node': str(edge.source_id),
        'target_node': str(edge.target_id),
        'type': edge.relationship_type,
        'attributes': edge.attributes if edge.attributes else {},
        'sentence': edge.sentence,
        'created_at': edge.created_at.isoformat() if edge.created_at else None,
        'updated_at': edge.updated_at.isoformat() if edge.updated_at else None,
        'confidence': edge.confidence,
        'importance': edge.importance,
        'source': edge.source,
    }

@graph_api.route('/api/graph', methods=['GET'])
def get_graph_data():
    session = get_session()
    try:
        nodes = session.query(Node).all()
        degrees = _degree_map_for(session, [n.id for n in nodes])
        node_data = [_serialize_node(n, degree=degrees.get(n.id, 0)) for n in nodes]

        edges = session.query(Edge).all()
        edge_data = [_serialize_edge(e) for e in edges]

        logger.debug("Returning %d nodes and %d edges", len(node_data), len(edge_data))

        return jsonify({
            'nodes': node_data,
            'edges': edge_data,
            'timestamp': to_rfc3339_z(utc_now())
        })
    finally:
        session.close()

@graph_api.route('/api/graph/node-types', methods=['GET'])
def get_node_types():
    """Get all node types for filtering"""
    return jsonify([{
        'node_type': nt,
        'json_schema': {}
    } for nt in NODE_TYPES])

@graph_api.route('/api/graph/edge-types', methods=['GET'])
def get_edge_types():
    session = get_session()
    try:
        edge_types = session.query(distinct(Edge.relationship_type)).order_by(Edge.relationship_type).all()
        return jsonify([{
            'type_name': et[0],
            'json_schema': {}
        } for et in edge_types if et[0]])
    finally:
        session.close()

@graph_api.route('/api/graph/search', methods=['GET'])
def search_graph():
    query = request.args.get('q', '').lower()
    node_type = request.args.get('node_type', '')
    edge_type = request.args.get('edge_type', '')

    session = get_session()
    try:
        node_query = session.query(Node)
        if query:
            node_query = node_query.filter(Node.label.ilike(f'%{query}%'))
        if node_type:
            node_query = node_query.filter(Node.node_type == node_type)

        nodes = node_query.all()
        degrees = _degree_map_for(session, [n.id for n in nodes])
        node_data = [_serialize_node(n, degree=degrees.get(n.id, 0)) for n in nodes]

        edge_query = session.query(Edge)
        if query:
            edge_query = edge_query.filter(Edge.sentence.ilike(f'%{query}%'))
        if edge_type:
            edge_query = edge_query.filter(Edge.relationship_type == edge_type)

        edges = edge_query.all()
        edge_data = [_serialize_edge(e) for e in edges]

        return jsonify({'nodes': node_data, 'edges': edge_data})
    finally:
        session.close()

@graph_api.route('/api/graph/node/<node_id>', methods=['GET'])
def get_node(node_id):
    session = get_session()
    try:
        node = session.query(Node).filter(Node.id == node_id).first()
        if not node:
            return jsonify({'error': 'Node not found'}), 404

        outgoing = session.query(Edge).filter(Edge.source_id == node_id).all()
        incoming = session.query(Edge).filter(Edge.target_id == node_id).all()

        return jsonify({
            'node': _serialize_node(node, degree=len(outgoing) + len(incoming)),
            'outgoing_edges': len(outgoing),
            'incoming_edges': len(incoming)
        })
    finally:
        session.close()

@graph_api.route('/api/graph/edge/<edge_id>', methods=['GET'])
def get_edge(edge_id):
    session = get_session()
    try:
        edge = session.query(Edge).filter(Edge.id == edge_id).first()
        if not edge:
            return jsonify({'error': 'Edge not found'}), 404
        return jsonify(_serialize_edge(edge))
    finally:
        session.close()

@graph_api.route('/api/graph/stats', methods=['GET'])
def get_stats():
    """Get graph statistics"""
    session = get_session()
    try:
        total_nodes = session.query(Node).count()
        total_edges = session.query(Edge).count()
        
        # Node type distribution
        node_types = {}
        for nt in NODE_TYPES:
            count = session.query(Node).filter(Node.node_type == nt).count()
            if count > 0:
                node_types[nt] = count
        
        # Edge type distribution
        edge_types = session.query(Edge.relationship_type, func.count(Edge.id)).group_by(Edge.relationship_type).all()
        edge_type_dist = {et[0]: et[1] for et in edge_types if et[0]}
        
        return jsonify({
            'total_nodes': total_nodes,
            'total_edges': total_edges,
            'node_types': node_types,
            'edge_types': edge_type_dist
        })
    finally:
        session.close()

@graph_api.route('/api/graph/node/<node_id>', methods=['DELETE'])
def delete_node(node_id):
    """Delete a node and its connected edges"""
    session = get_session()
    try:
        node = session.query(Node).filter(Node.id == node_id).first()
        if not node:
            return jsonify({'error': 'Node not found'}), 404
        
        # Delete connected edges first (CASCADE should handle this, but let's be explicit)
        session.query(Edge).filter((Edge.source_id == node_id) | (Edge.target_id == node_id)).delete()
        
        # Delete the node
        session.delete(node)
        session.commit()
        
        return jsonify({'message': 'Node deleted successfully'}), 200
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@graph_api.route('/api/graph/edge/<edge_id>', methods=['DELETE'])
def delete_edge(edge_id):
    """Delete an edge"""
    session = get_session()
    try:
        edge = session.query(Edge).filter(Edge.id == edge_id).first()
        if not edge:
            return jsonify({'error': 'Edge not found'}), 404
        
        session.delete(edge)
        session.commit()
        
        return jsonify({'message': 'Edge deleted successfully'}), 200
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@graph_api.route('/api/graph/node/<node_id>', methods=['PUT'])
def update_node(node_id):
    """Update node properties"""
    session = get_session()
    try:
        node = session.query(Node).filter(Node.id == node_id).first()
        if not node:
            return jsonify({'error': 'Node not found'}), 404
        
        data = request.json
        
        # Update allowed fields
        if 'label' in data:
            node.label = data['label']
        if 'description' in data:
            node.description = data['description']
        if 'category' in data:
            node.category = data['category']
        if 'aliases' in data:
            node.aliases = data['aliases']
        if 'attributes' in data:
            node.attributes = data['attributes']
        if 'confidence' in data:
            node.confidence = data['confidence']
        if 'importance' in data:
            node.importance = data['importance']
        if 'semantic_label' in data:
            node.semantic_label = data['semantic_label']
        if 'original_sentence' in data:
            node.original_sentence = data['original_sentence']
        if 'hash_tags' in data:
            # Accept list directly; tolerate a comma-separated string from form edits.
            raw_tags = data['hash_tags']
            if isinstance(raw_tags, str):
                raw_tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
            if isinstance(raw_tags, list):
                node.hash_tags = [str(t).strip() for t in raw_tags if str(t).strip()]

        node.updated_at = utc_now().replace(tzinfo=None)
        session.commit()
        
        return jsonify({
            'id': str(node.id),
            'label': node.label,
            'type': node.node_type,
            'category': node.category,
            'aliases': node.aliases if node.aliases else [],
            'description': node.description,
            'attributes': node.attributes if node.attributes else {},
            'confidence': node.confidence,
            'importance': node.importance,
            'semantic_label': node.semantic_label,
            'updated_at': node.updated_at.isoformat() if node.updated_at else None
        })
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()

@graph_api.route('/api/graph/hubs', methods=['GET'])
def get_hubs():
    """Return nodes whose degree (in + out edges) >= min_degree.

    Query params:
      min_degree  int  default 1 (skip true orphans)

    Visibility filtering by importance happens CLIENT-SIDE in the visualizer —
    this endpoint returns the full graph (above the orphan floor) so the
    slider can re-filter without re-fetching.
    """
    min_degree = int(request.args.get('min_degree', 1))
    session = get_session()
    try:
        from sqlalchemy import text
        rows = session.execute(text(
            """
            SELECT node_id, SUM(cnt) AS degree FROM (
                SELECT source_id AS node_id, COUNT(*) AS cnt
                  FROM kg_edge_metadata GROUP BY source_id
                UNION ALL
                SELECT target_id AS node_id, COUNT(*) AS cnt
                  FROM kg_edge_metadata GROUP BY target_id
            ) sub GROUP BY node_id HAVING SUM(cnt) >= :min_degree
            """
        ), {'min_degree': min_degree}).fetchall()

        node_ids = [r[0] for r in rows]
        degree_map = {r[0]: r[1] for r in rows}

        if not node_ids:
            return jsonify({'nodes': [], 'edges': [], 'timestamp': to_rfc3339_z(utc_now())})

        nodes = session.query(Node).filter(Node.id.in_(node_ids)).all()
        node_data = [_serialize_node(n, degree=int(degree_map.get(n.id, 0))) for n in nodes]
        known_node_ids = {str(n.id) for n in nodes}

        # Edges where AT LEAST one endpoint is a known node. The other endpoint
        # may be a pod URI (kg_node_metadata has no pod rows). We surface pods
        # as Pod-typed nodes from pod_store and DROP any edges whose endpoints
        # remain unknown (dangling refs that would crash the frontend renderer).
        visible_ids = set(node_ids)
        edges = session.query(Edge).filter(
            (Edge.source_id.in_(visible_ids)) | (Edge.target_id.in_(visible_ids))
        ).all()

        # Collect non-node endpoints and try to resolve them as pods
        pod_node_data = []
        added_endpoints: set[str] = set()
        for e in edges:
            for endpoint in (str(e.source_id), str(e.target_id)):
                if endpoint in known_node_ids or endpoint in added_endpoints:
                    continue
                pod = _serialize_pod_uri_as_node(endpoint)
                if pod is not None:
                    pod_node_data.append(pod)
                    added_endpoints.add(endpoint)
                    known_node_ids.add(endpoint)

        # Drop dangling edges (endpoint that's neither a real node nor a pod).
        edges = [
            e for e in edges
            if str(e.source_id) in known_node_ids and str(e.target_id) in known_node_ids
        ]
        edge_data = [_serialize_edge(e) for e in edges]
        node_data.extend(pod_node_data)

        logger.debug("Hubs (min_degree=%d): %d nodes (%d pods), %d edges",
                     min_degree, len(node_data), len(pod_node_data), len(edge_data))

        return jsonify({
            'nodes': node_data,
            'edges': edge_data,
            'timestamp': to_rfc3339_z(utc_now())
        })
    finally:
        session.close()


@graph_api.route('/api/graph/subgraph', methods=['GET'])
def get_subgraph():
    """Return the focal node and everything reachable within N hops.

    Query params:
      node_id  str  required
      depth    int  default 1. 0 = just the node. 1 = direct neighbors.
                    2 = neighbors of neighbors. Clamped to [0, 3].
    """
    node_id = request.args.get('node_id')
    if not node_id:
        return jsonify({'error': 'node_id is required'}), 400
    try:
        depth = int(request.args.get('depth', 1))
    except (TypeError, ValueError):
        depth = 1
    # Cap aggressively — depth 3 can return thousands of nodes on hub-like
    # starting points, which the frontend renderer struggles with.
    depth = max(0, min(3, depth))

    session = get_session()
    try:
        focal = session.query(Node).filter(Node.id == node_id).first()
        if not focal:
            return jsonify({'error': 'Node not found'}), 404

        # BFS: grow the visible set one hop at a time. Keep every edge we
        # traverse so we can return them all (those with both endpoints in
        # the final visible set).
        visible: set[str] = {node_id}
        all_edges: list[Edge] = []
        frontier: set[str] = {node_id}
        for _ in range(depth):
            if not frontier:
                break
            hop_edges = session.query(Edge).filter(
                or_(Edge.source_id.in_(frontier), Edge.target_id.in_(frontier))
            ).all()
            next_frontier: set[str] = set()
            for e in hop_edges:
                all_edges.append(e)
                if e.source_id not in visible:
                    next_frontier.add(e.source_id)
                if e.target_id not in visible:
                    next_frontier.add(e.target_id)
            visible |= next_frontier
            frontier = next_frontier

        nodes = session.query(Node).filter(Node.id.in_(visible)).all()
        degrees = _degree_map_for(session, [n.id for n in nodes])
        node_data = [_serialize_node(n, degree=degrees.get(n.id, 0)) for n in nodes]

        # Pod URIs in `visible` won't come back from the Node query (no mirror).
        # Render them as Pod-typed nodes from pod_store so the frontend doesn't
        # see orphan edges. Degree is computed via the existing helper, which
        # works for any id string.
        resolved_ids = {n.id for n in nodes}
        unresolved = visible - resolved_ids
        if unresolved:
            pod_degrees = _degree_map_for(session, list(unresolved))
            for pid in unresolved:
                pod_node = _serialize_pod_uri_as_node(pid, degree=pod_degrees.get(pid, 0))
                if pod_node is not None:
                    node_data.append(pod_node)
                    resolved_ids.add(pid)

        # Deduplicate edges (BFS can see the same edge from both endpoints
        # on successive hops) and keep only those whose endpoints both resolved
        # (kg_node OR pod-shim). Edges to genuinely dangling ids are dropped.
        seen_edge_ids: set = set()
        edge_data: list = []
        for e in all_edges:
            if e.id in seen_edge_ids:
                continue
            if e.source_id not in resolved_ids or e.target_id not in resolved_ids:
                continue
            seen_edge_ids.add(e.id)
            edge_data.append(_serialize_edge(e))

        logger.debug(
            "Subgraph for %s (depth=%d): %d nodes, %d edges",
            node_id, depth, len(node_data), len(edge_data),
        )

        return jsonify({
            'nodes': node_data,
            'edges': edge_data,
            'focal_node_id': node_id,
            'depth': depth,
            'timestamp': to_rfc3339_z(utc_now())
        })
    finally:
        session.close()


@graph_api.route('/api/graph/degree-distribution', methods=['GET'])
def get_degree_distribution():
    """Return degree counts bucketed for frontend slider calibration."""
    session = get_session()
    try:
        from sqlalchemy import text
        rows = session.execute(text(
            """
            SELECT node_id, SUM(cnt) AS degree FROM (
                SELECT source_id AS node_id, COUNT(*) AS cnt
                  FROM kg_edge_metadata GROUP BY source_id
                UNION ALL
                SELECT target_id AS node_id, COUNT(*) AS cnt
                  FROM kg_edge_metadata GROUP BY target_id
            ) sub GROUP BY node_id
            """
        )).fetchall()

        buckets = {}
        for _, degree in rows:
            d = int(degree)
            buckets[d] = buckets.get(d, 0) + 1

        # Also return count of nodes visible at each threshold (cumulative from high to low)
        thresholds = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20]
        threshold_counts = {}
        for t in thresholds:
            threshold_counts[t] = sum(cnt for deg, cnt in buckets.items() if deg >= t)

        return jsonify({
            'buckets': buckets,
            'threshold_counts': threshold_counts,
            'total_nodes_with_edges': len(rows),
        })
    finally:
        session.close()


@graph_api.route('/api/graph/edge/<edge_id>', methods=['PUT'])
def update_edge(edge_id):
    """Update edge properties"""
    session = get_session()
    try:
        edge = session.query(Edge).filter(Edge.id == edge_id).first()
        if not edge:
            return jsonify({'error': 'Edge not found'}), 404
        
        data = request.json
        
        # Update allowed fields
        if 'relationship_type' in data:
            edge.relationship_type = data['relationship_type']
        if 'relationship_descriptor' in data:
            edge.relationship_descriptor = data['relationship_descriptor']
        if 'attributes' in data:
            edge.attributes = data['attributes']
        if 'sentence' in data:
            edge.sentence = data['sentence']
        if 'confidence' in data:
            edge.confidence = data['confidence']
        if 'importance' in data:
            edge.importance = data['importance']
        
        edge.updated_at = utc_now().replace(tzinfo=None)
        session.commit()
        
        return jsonify({
            'id': str(edge.id),
            'source_node': str(edge.source_id),
            'target_node': str(edge.target_id),
            'type': edge.relationship_type,
            'relationship_descriptor': edge.relationship_descriptor,
            'attributes': edge.attributes if edge.attributes else {},
            'sentence': edge.sentence,
            'confidence': edge.confidence,
            'importance': edge.importance,
            'updated_at': edge.updated_at.isoformat() if edge.updated_at else None
        })
    except Exception as e:
        session.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        session.close()
