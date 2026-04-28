from pydantic import BaseModel


class AgentForm(BaseModel):
    """The full markdown page as a single string.

    Must include the original frontmatter verbatim and end with the
    See also / KG gaps sections as the rough page provided them.
    """
    page_markdown: str
