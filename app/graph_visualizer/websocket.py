from flask_socketio import SocketIO, emit, join_room, leave_room
from flask import request
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node, Edge
from app.models.base import get_session
from datetime import datetime, timezone

from app.assistant.utils.time_utils import to_rfc3339_z, utc_now

class GraphWebSocket:
    def __init__(self, socketio: SocketIO):
        self.socketio = socketio
        self.setup_handlers()
    
    def setup_handlers(self):
        @self.socketio.on('connect')
        def handle_connect():
            print(f"Client connected: {request.sid}")
            emit('connected', {'message': 'Connected to graph updates'})
        
        @self.socketio.on('disconnect')
        def handle_disconnect():
            print(f"Client disconnected: {request.sid}")
        
        @self.socketio.on('join_graph')
        def handle_join_graph(data=None):
            """Join the graph room for updates"""
            room = 'graph_updates'
            join_room(room)
            emit('joined_graph', {'message': 'Joined graph updates room'})
        
        @self.socketio.on('leave_graph')
        def handle_leave_graph(data=None):
            """Leave the graph room"""
            room = 'graph_updates'
            leave_room(room)
            emit('left_graph', {'message': 'Left graph updates room'})
        
        @self.socketio.on('request_graph_data')
        def handle_request_graph_data():
            """Send current graph data to client"""
            try:
                session = get_session()
                try:
                    # Get all nodes
                    nodes = session.query(Node).all()
                    node_data = []
                    for node in nodes:
                        node_data.append({
                            'id': str(node.id),
                            'label': node.label,
                            'type': node.node_type,
                            'properties': node.attributes or {},
                            'confidence': node.confidence,
                            'strength': node.importance or 0.5
                        })
                    
                    # Get all edges
                    edges = session.query(Edge).all()
                    edge_data = []
                    for edge in edges:
                        edge_data.append({
                            'id': str(edge.id),
                            'source': str(edge.source_id),
                            'target': str(edge.target_id),
                            'type': edge.relationship_type,
                            'properties': edge.attributes or {},
                            'weight': edge.importance or 0.5
                        })
                    
                    emit('graph_data', {
                        'nodes': node_data,
                        'edges': edge_data,
                        'timestamp': to_rfc3339_z(utc_now())
                    })
                finally:
                    session.close()
            except Exception as e:
                emit('error', {'message': f'Error fetching graph data: {str(e)}'})
    
    def broadcast_node_added(self, node_data):
        """Broadcast when a new node is added"""
        self.socketio.emit('node_added', node_data, room='graph_updates')
    
    def broadcast_node_updated(self, node_data):
        """Broadcast when a node is updated"""
        self.socketio.emit('node_updated', node_data, room='graph_updates')
    
    def broadcast_node_deleted(self, node_id):
        """Broadcast when a node is deleted"""
        self.socketio.emit('node_deleted', {'id': node_id}, room='graph_updates')
    
    def broadcast_edge_added(self, edge_data):
        """Broadcast when a new edge is added"""
        self.socketio.emit('edge_added', edge_data, room='graph_updates')
    
    def broadcast_edge_updated(self, edge_data):
        """Broadcast when an edge is updated"""
        self.socketio.emit('edge_updated', edge_data, room='graph_updates')
    
    def broadcast_edge_deleted(self, edge_id):
        """Broadcast when an edge is deleted"""
        self.socketio.emit('edge_deleted', {'id': edge_id}, room='graph_updates')
    
    def broadcast_graph_stats(self, stats):
        """Broadcast updated graph statistics"""
        self.socketio.emit('graph_stats_updated', stats, room='graph_updates') 