from typing import List, Optional

from pydantic import BaseModel, Field


class send_email_args(BaseModel):
    to: str
    subject: Optional[str]
    body: Optional[str]
    account_id: Optional[str] = None
    pod_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional pod URIs (e.g. ['datapod:image:abc...']) to attach as files. Each pod's backing file is read from metadata.stored_path and attached. Use pod_search to find pod_ids first.",
    )


class send_email_arguments(BaseModel):
    tool_name: str
    arguments: send_email_args