from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    updated_spec: str = Field(
        description="The complete updated task spec markdown, including YAML frontmatter. Ready to write to disk.",
    )
    changes_summary: str = Field(
        description="Brief summary of what changed and why (2-5 sentences).",
    )
