from pydantic import BaseModel, Field

class find_node_args(BaseModel):
    """
    Arguments for the find_node agent.
    """
    agent_task: str = Field(
        ...,
        description="The search task or query for finding nodes in the knowledge graph. Can be natural language like 'Find nodes missing temporal info' or 'Find events related to weddings'."
    )

class find_node_arguments(BaseModel):
    arguments: find_node_args
