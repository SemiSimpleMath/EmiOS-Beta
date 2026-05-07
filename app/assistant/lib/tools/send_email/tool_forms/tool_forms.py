from typing import List, Optional

from pydantic import BaseModel, Field


class send_email_args(BaseModel):
    to: str
    subject: Optional[str]
    body: Optional[str]
    pod_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional pod URIs (e.g. ['datapod:image:abc...']) to attach as files. Each pod's backing file is read from metadata.stored_path and attached. Use pod_search to find pod_ids first.",
    )
    inline_pod_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional list of pod URIs to PASTE INLINE into the email body (instead of attaching as a file). For each pod the tool fetches the body and appends a delimited block to `body` — for email pods that's the standard `----- Forwarded message -----` block; for other body-only kinds it's a `---` separator + one-liner + body. The agent never reads the body — fetch happens at tool execution. Use `inline_pod_ids` when the recipient should see the content directly in the email body (typical for forwarding emails or pasting a transcript); use `pod_ids` when they should get an attachment to download.",
    )


class send_email_arguments(BaseModel):
    tool_name: str
    arguments: send_email_args