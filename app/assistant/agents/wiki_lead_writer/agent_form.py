from pydantic import BaseModel, Field


class AgentForm(BaseModel):
    lead_markdown: str = Field(
        description=(
            "The Wikipedia-style lead for the article: 1-2 short paragraphs of "
            "markdown text, opening with the entity's name in bold. No headings, "
            "no horizontal rule, no leading or trailing blank lines. Returns "
            "only the lead — frontmatter, title, and body sections are added "
            "by the caller."
        ),
    )
