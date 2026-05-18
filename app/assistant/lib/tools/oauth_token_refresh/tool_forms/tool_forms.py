"""Pydantic agent_form for oauth_token_refresh.

Refreshes the access_token projection of an auth.oauth pod using its
stored refresh_token. The agent never sees either token — courier-scoped
fetch + POST + env-var update happens in the tool.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class oauth_token_refresh_args(BaseModel):
    pod_id: str
    # If True, refresh even if expiry_iso says the current token is still
    # valid. Use sparingly — most callers should let the tool short-circuit
    # when the token isn't near expiry.
    force: bool = False


class oauth_token_refresh_arguments(BaseModel):
    tool_name: str
    arguments: oauth_token_refresh_args
