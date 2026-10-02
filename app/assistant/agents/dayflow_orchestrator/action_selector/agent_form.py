from typing import List

from pydantic import BaseModel, Field, model_validator


class AgentForm(BaseModel):
    reasoning: str = Field(
        description="One sentence explaining why you chose this item.",
    )
    acted_on_item_ids: List[str] = Field(
        default_factory=list,
        description="The ONE item id you picked, exactly as shown in the actionable list. Empty only "
        "when no_op_tf is true.",
    )
    no_op_tf: bool = Field(
        default=False,
        description="Set true when there is nothing to dispatch this tick.",
    )

    @model_validator(mode="after")
    def _one_pick_or_no_op(self):
        if self.no_op_tf and self.acted_on_item_ids:
            raise ValueError("no_op_tf=true takes no item id")
        if not self.no_op_tf and len(self.acted_on_item_ids) != 1:
            raise ValueError("pick exactly one item id, or set no_op_tf=true")
        return self
