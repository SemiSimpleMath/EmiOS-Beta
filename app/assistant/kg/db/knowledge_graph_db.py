# knowledge_graph_db.py
# 
# COMPATIBILITY LAYER: This file re-exports from knowledge_graph_db_sqlite.py
# 
# History:
#   - Original file defined Node/Edge with __tablename__ = 'nodes'/'edges'
#   - PostgreSQL to SQLite migration renamed tables to 'kg_node_metadata'/'kg_edge_metadata'
#   - knowledge_graph_db_sqlite.py was created with the correct table names
#   - This file now re-exports from _sqlite.py so all existing imports continue to work
#
# The actual tables in the database are:
#   - kg_node_metadata (not 'nodes')
#   - kg_edge_metadata (not 'edges')

from app.assistant.kg.db.knowledge_graph_db_sqlite import (
    Node,
    Edge,
    MessageSourceMapping,
    NODE_TYPES,
    initialize_knowledge_graph_db,
    drop_knowledge_graph_db,
    reset_knowledge_graph_db,
)

# Re-export get_session for backwards compatibility
from app.models.base import get_session

# For any code that imports * from this module
__all__ = [
    'Node',
    'Edge', 
    'MessageSourceMapping',
    'NODE_TYPES',
    'initialize_knowledge_graph_db',
    'drop_knowledge_graph_db',
    'reset_knowledge_graph_db',
    'get_session',
]
