from enum import Enum
from typing import List, Dict, Any
from pydantic import BaseModel, Field

class Node(BaseModel):
    """A node to be created in the knowledge graph"""
    node_type: str
    temp_id: str
    label: str
    category: str
    sentence: str
    reasoning: str

class Edge(BaseModel):
    """An edge/relationship to be created in the knowledge graph"""
    temp_id: str
    relationship_type: str
    label: str
    source: str
    target: str
    bidirectional: bool
    sentence: str

class AgentForm(BaseModel):
    """Form for node/edge extraction results"""
    nodes: List[Node]
    edges: List[Edge]
