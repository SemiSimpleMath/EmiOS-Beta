from typing import Dict, List
from pydantic import BaseModel, ConfigDict, Field


class NamespaceTags(BaseModel):
    """Tags for one node within one namespace."""
    model_config = ConfigDict(extra="forbid")

    namespace: str = Field(
        ...,
        description="Tag namespace, e.g. 'card' or 'wiki'.",
    )
    sections: List[str] = Field(
        default_factory=list,
        description=(
            "Section keys this node belongs to within the namespace. "
            "Multi-tag allowed (a fact can legitimately appear in multiple "
            "sections). Empty list = node is not card/wiki-worthy in this "
            "namespace."
        ),
    )


class TaggedNode(BaseModel):
    """All namespace tags for one input node, keyed by the input number."""
    model_config = ConfigDict(extra="forbid")

    number: int = Field(..., description="The number from the input nodes_block.")
    tags: List[NamespaceTags] = Field(
        default_factory=list,
        description=(
            "One entry per namespace from the input. Include every "
            "namespace from the input (with sections=[] if the node "
            "doesn't belong anywhere in that namespace)."
        ),
    )


class AgentForm(BaseModel):
    """kg_node_section_tagger output: tags per node per namespace, in one call."""
    model_config = ConfigDict(extra="forbid")

    results: List[TaggedNode] = Field(
        ...,
        description=(
            "One TaggedNode per input node, preserving the input numbers. "
            "Every input node must appear exactly once."
        ),
    )
    reason: str = Field(
        ...,
        description="Brief overall justification (<= 30 words).",
    )
