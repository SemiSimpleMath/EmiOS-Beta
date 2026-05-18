"""Materializer for `auth.bearer` pod type.

Generic credential pod for opaque tokens — Bearer tokens, API keys,
PATs, OAuth access tokens, anything that's "a long string the agent
should never see." The full value lives in an env var; the materializer
records the pointer plus a couple of low-authority display projections.

Projections:

  full         100 (courier-only)   the actual token   via env_ref
  prefix        50 (chat)           first 6 chars + "..." for disambiguation
  redacted      10 (public)         fixed "***"  display-anywhere placeholder
  format        10 (public)         "<token kind>, N chars" descriptor

Usage:

    os.environ["EMI_POD_WHOOP_TOKEN"] = "..."
    PodStore().put_secret_pod(
        pod_type="auth.bearer",
        owner_subject_id="jukka",
        name="WHOOP API token",
        env_ref="EMI_POD_WHOOP_TOKEN",
        scope=scope,
    )

Then in an http_request call:

    headers={"Authorization": f"Bearer datapod:auth.bearer:{pod_id}/full"}

(Or wrap the whole header bundle in a separate pod once that v1.2 feature lands.)
"""
from __future__ import annotations

from typing import List

from app.assistant.pod_store.authority import (
    AUTH_PUBLIC, AUTH_CHAT, AUTH_COURIER,
)
from app.assistant.pod_store.materializers import ProjectionSpec, register


# Sanity bounds. Real tokens range from ~20 chars (some session IDs) to a few
# hundred (OAuth JWTs). The lower bound rules out empty/whitespace bugs; the
# upper bound prevents accidental file dumps.
_MIN_LEN = 8
_MAX_LEN = 4096


def materialize(*, raw_value: str, env_ref: str, **_kwargs) -> List[ProjectionSpec]:
    """Compute projections from a raw bearer token / API key.

    Args:
        raw_value: the token string as read from the env var. Trimmed of
            edge whitespace; rejected if too short or too long.
        env_ref: the env var name where raw_value lives. Recorded on the
            full-projection row so courier reads can re-resolve.

    Raises ValueError on empty/too-short/too-long input.
    """
    if not raw_value:
        raise ValueError("auth.bearer materialize: raw_value is empty")
    val = raw_value.strip()
    if len(val) < _MIN_LEN:
        raise ValueError(
            f"auth.bearer materialize: raw_value too short "
            f"({len(val)} chars; need at least {_MIN_LEN})"
        )
    if len(val) > _MAX_LEN:
        raise ValueError(
            f"auth.bearer materialize: raw_value too long "
            f"({len(val)} chars; max {_MAX_LEN})"
        )

    # Prefix for disambiguation in chat-tier rendering. 6 chars is enough
    # to distinguish OpenAI ("sk-pro"), GitHub ("ghp_..."), WHOOP, etc.
    # without exposing the full token.
    prefix = val[:6] + "..."

    return [
        ProjectionSpec(
            projection_name="full",
            min_authority=AUTH_COURIER,
            storage_kind="env",
            env_ref=env_ref,
        ),
        ProjectionSpec(
            projection_name="prefix",
            min_authority=AUTH_CHAT,
            storage_kind="plain",
            plain_value=prefix,
        ),
        ProjectionSpec(
            projection_name="redacted",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value="***",
        ),
        ProjectionSpec(
            projection_name="format",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value=f"bearer token / API key, {len(val)} chars",
        ),
    ]


register("auth.bearer", materialize)
