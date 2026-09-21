"""Validated, durable response choices for contextual Dayflow tickets."""
from typing import Literal
from pydantic import BaseModel, Field, field_validator, TypeAdapter


class ResponseChoice(BaseModel):
    label: str = Field(min_length=1, max_length=24)
    meaning: Literal["acknowledge", "yes", "no", "approve", "decline", "will_do", "done", "handle_myself", "later"]
    scope: str = Field(min_length=1, max_length=240)

    @field_validator("label")
    @classmethod
    def short_statement(cls, value):
        if value != value.strip() or len(value.split()) > 3 or any(c in value for c in "?？\r\n"):
            raise ValueError("Labels require at most 3 words and no questions or newlines")
        return value


def validate_choices(values):
    choices = TypeAdapter(list[ResponseChoice]).validate_python(values)
    if not 1 <= len(choices) <= 3:
        raise ValueError("A ticket requires 1 to 3 choices")
    if len({c.label.casefold() for c in choices}) != len(choices):
        raise ValueError("Choice labels must be distinct")
    return [{"id": f"choice_{i + 1}", **choice.model_dump()} for i, choice in enumerate(choices)]
