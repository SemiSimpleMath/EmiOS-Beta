"""Materializer for `auth.oauth` pod type.

OAuth 2.0 credential pod that pairs an access token with the metadata
needed to refresh it. Designed to work with the `oauth_token_refresh`
tool — that tool reads the refresh token + provider config at courier
scope, POSTs to the provider's refresh endpoint, and writes the new
access token back to its env var. The http_request tool then picks up
the rotated value automatically on its next call.

Projections:

  access_token   100 (courier-only)   the active access token   via access_token_env_ref
  refresh_token  100 (courier-only)   long-lived refresh token  via refresh_token_env_ref
  expiry         50  (chat)           ISO timestamp of expected expiry   plain
  provider       10  (public)         provider label (spotify, google, …)  plain
  redacted       10  (public)         fixed "***"                          plain

Metadata fields (read by oauth_token_refresh):
  refresh_url           — provider's token endpoint
  access_token_env_ref  — env var holding the current access token (rotated on refresh)
  refresh_token_env_ref — env var holding the refresh token
  client_id_env_ref     — env var with OAuth client_id (or None for public clients)
  client_secret_env_ref — env var with OAuth client_secret (or None for public clients)
  provider              — string label (`"spotify"`, `"google"`, `"whoop"`, …)
  scope                 — OAuth scopes string (optional, sent on refresh)

Usage:

    os.environ["EMI_POD_SPOTIFY_ACCESS"]  = "<current access token>"
    os.environ["EMI_POD_SPOTIFY_REFRESH"] = "<refresh token>"
    os.environ["EMI_POD_SPOTIFY_CLIENT_ID"] = "..."
    os.environ["EMI_POD_SPOTIFY_CLIENT_SECRET"] = "..."

    PodStore().put_secret_pod(
        pod_type="auth.oauth",
        owner_subject_id="jukka",
        name="Spotify OAuth credentials",
        env_ref="EMI_POD_SPOTIFY_ACCESS",  # primary env_ref = access token
        scope=scope,
        kwargs={
            "refresh_token_env_ref": "EMI_POD_SPOTIFY_REFRESH",
            "client_id_env_ref":     "EMI_POD_SPOTIFY_CLIENT_ID",
            "client_secret_env_ref": "EMI_POD_SPOTIFY_CLIENT_SECRET",
            "refresh_url":           "https://accounts.spotify.com/api/token",
            "provider":              "spotify",
            "expiry_iso":            "2026-05-19T20:00:00Z",
        },
    )

Then in an http_request call:

    headers={"Authorization": "Bearer datapod:auth.oauth:<pod_id>/access_token"}

When the access token expires, an agent invokes oauth_token_refresh(pod_id);
the env var is updated in place, and the next http_request picks it up.
"""
from __future__ import annotations

from typing import List

from app.assistant.pod_store.authority import (
    AUTH_PUBLIC, AUTH_CHAT, AUTH_COURIER,
)
from app.assistant.pod_store.materializers import ProjectionSpec, register


_MIN_TOKEN_LEN = 8
_MAX_TOKEN_LEN = 8192


def materialize(*, raw_value: str, env_ref: str, **kwargs) -> List[ProjectionSpec]:
    """Compute projections for an OAuth credential pod.

    Args:
        raw_value: the current access token (read from env_ref).
        env_ref: env var name where the access token lives. Rotated by
            oauth_token_refresh.
        kwargs (required for OAuth):
            refresh_token_env_ref: env var holding the refresh token
            refresh_url: provider's token endpoint
            provider: provider label string
        kwargs (optional):
            client_id_env_ref / client_secret_env_ref: env vars with client creds
            expiry_iso: ISO timestamp of when the access token expires
            scope: OAuth scope string

    Raises ValueError on missing required kwargs.
    """
    if not raw_value:
        raise ValueError("auth.oauth materialize: raw_value (access_token) is empty")
    val = raw_value.strip()
    if len(val) < _MIN_TOKEN_LEN or len(val) > _MAX_TOKEN_LEN:
        raise ValueError(
            f"auth.oauth materialize: access token length {len(val)} out of "
            f"bounds [{_MIN_TOKEN_LEN}, {_MAX_TOKEN_LEN}]"
        )

    refresh_token_env_ref = kwargs.get("refresh_token_env_ref")
    refresh_url = kwargs.get("refresh_url")
    provider = kwargs.get("provider")
    if not refresh_token_env_ref:
        raise ValueError("auth.oauth materialize: refresh_token_env_ref is required")
    if not refresh_url:
        raise ValueError("auth.oauth materialize: refresh_url is required")
    if not provider:
        raise ValueError("auth.oauth materialize: provider label is required")

    expiry_iso = kwargs.get("expiry_iso") or "unknown"

    return [
        # Access token: env-backed, rotated by oauth_token_refresh.
        ProjectionSpec(
            projection_name="access_token",
            min_authority=AUTH_COURIER,
            storage_kind="env",
            env_ref=env_ref,
        ),
        # Refresh token: env-backed; long-lived, courier-only.
        ProjectionSpec(
            projection_name="refresh_token",
            min_authority=AUTH_COURIER,
            storage_kind="env",
            env_ref=refresh_token_env_ref,
        ),
        # Expiry — chat-tier so agents can decide whether to refresh.
        ProjectionSpec(
            projection_name="expiry",
            min_authority=AUTH_CHAT,
            storage_kind="plain",
            plain_value=expiry_iso,
        ),
        # Provider label — public, for "which OAuth pod is this?" disambiguation.
        ProjectionSpec(
            projection_name="provider",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value=str(provider),
        ),
        # Redacted display.
        ProjectionSpec(
            projection_name="redacted",
            min_authority=AUTH_PUBLIC,
            storage_kind="plain",
            plain_value="***",
        ),
    ]


register("auth.oauth", materialize)
