from pydantic import BaseModel


class AgentForm(BaseModel):
    """
    Delegator does not rely on structured output in this manager.
    This exists to satisfy the agent loader's expected file layout.
    """

    ok: bool = True

