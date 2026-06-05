from typing import List, Optional

from pydantic import BaseModel, Field


class send_email_args(BaseModel):
    to: str
    subject: Optional[str]
    body: Optional[str]
    from_account: Optional[str] = Field(
        default=None,
        description=(
            "Which account the email is sent FROM. Omit to use the current scope's "
            "actor (acting_as) — normally the user's account. Pass 'self' to send from "
            "the assistant's own Gmail (acting as the assistant, not the user); pass "
            "'user' to force the user's account. "
            "Rule of thumb — who is the actor? "
            "• Email TO the user (notifications, reminders, artifacts for them) → 'self'. "
            "• Email on the user's behalf to their contacts (replies, forwards) → 'user' "
            "  (the default — omit the arg). "
            "• Email the assistant sends as herself (her own signups/subscriptions, "
            "  public correspondence) → 'self'. "
            "See the email-as-self / email-as-user skills for edge cases. A specific "
            "account_id from configs/oauth_accounts.json is also accepted (advanced)."
        ),
    )
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