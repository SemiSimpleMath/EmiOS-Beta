from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


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
    @field_validator("from_account")
    @classmethod
    def _known_account(cls, v):
        """The closed vocabulary, enforced where every path validates.

        The planner never sees the alias doctrine (it reads the compact tool card),
        so it once filled this field with the user's raw EMAIL ADDRESS off an entity
        card — which rode the fast path (any string type-validates), reached the
        credentials layer, and popped an OAuth consent flow the registry gate had
        to block (2026-09-02). A validator failure here is not an error surface:
        the fast-path gate treats it as "slow path", which routes the call to the
        args agent — the one reader of the full documentation — in the same cycle.
        """
        if v is None or not str(v).strip():
            return v                       # omitted -> the scope's actor (the default)
        val = str(v).strip()
        if val in {"self", "user"}:
            return val
        from app.assistant.lib.google_auth import oauth_registry
        if oauth_registry.is_known_account(val):
            return val
        known = sorted(oauth_registry.list_accounts().keys())
        raise ValueError(
            f"from_account {val!r} is not 'self', 'user', or a registry account id "
            f"(known: {known}). Omit it to send as the user; 'self' sends as the "
            f"assistant's own account. Never pass a raw email address here."
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