from typing import Optional

from pydantic import BaseModel, Field


class kg_query_args(BaseModel):
    sql: str = Field(
        description=(
            "One SELECT or WITH ... SELECT statement against the live emi.db. "
            "PRAGMA table_info('<t>') and PRAGMA foreign_key_list('<t>') are also "
            "allowed for schema discovery. No semicolon-chained statements."
        ),
    )
    max_rows: Optional[int] = Field(
        default=200,
        description="Row cap (default 200, absolute max 5000).",
    )


class kg_query_arguments(BaseModel):
    tool_name: str
    arguments: kg_query_args


kg_query_arguments.model_rebuild()
