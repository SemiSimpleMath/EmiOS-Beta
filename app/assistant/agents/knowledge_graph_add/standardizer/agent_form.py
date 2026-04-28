from pydantic import BaseModel, Field
from typing import List
from app.assistant.kg_core.user_identity import PRIMARY_USER_NODE_LABEL


class AgentForm(BaseModel):
    semantic_label: str = Field(
        description="Human-readable, context-specific description of the node (e.g., 'Father (user's)', 'Likes sushi (user)')"
    )
    reasoning: str = Field(
        description="Brief explanation of the semantic label choice"
    )
