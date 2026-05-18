"""oauth_token_refresh — rotate the access_token of an auth.oauth pod.

Reads the refresh_token and client credentials at courier scope, POSTs to
the provider's refresh endpoint (URL stored in the pod's metadata), updates
the access_token env var in place. Tokens never enter the agent's transcript.

The next http_request that resolves `datapod:auth.oauth:<id>/access_token`
picks up the new value automatically — the env_ref pointer in the projection
row doesn't change, only the env var's contents.

## Short-circuit on near-expiry

By default the tool reads the pod's `expiry` projection (chat-tier) and skips
the refresh if the stored expiry is more than 60s in the future. Pass
`force=true` to refresh regardless.

## What v1.2 does NOT yet support

- Persistent storage of rotated tokens. Updates land in `os.environ`, which
  resets on Flask restart. For surviving restarts, the user must keep their
  .env / secret store updated. File-backed projection storage is the v1.3
  fix.
- Refresh-token rotation persistence. If the provider issues a NEW refresh
  token (some do), v1.2 updates os.environ but doesn't rewrite .env.
- Provider-specific quirks (Google requires `access_type=offline`,
  Microsoft requires `scope` echo, etc.). Standard RFC 6749 only for v1.2.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeToolPolicy,
    ToolMessage,
    ToolResult,
)

logger = get_logger(__name__)


_REFRESH_TIMEOUT_S = 15.0
# Min seconds remaining before the tool considers the token "near expiry"
# and proactively refreshes. Default: refresh when < 60s left.
_NEAR_EXPIRY_SECONDS = 60


class OauthTokenRefresh(BaseTool):
    """Refresh an auth.oauth pod's access_token."""

    requires_approval = False

    def __init__(self):
        super().__init__("oauth_token_refresh")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments") or {}
        pod_id = (args.get("pod_id") or "").strip()
        force = bool(args.get("force", False))

        if not pod_id:
            return make_tool_error(
                error_code="invalid_arguments",
                message="oauth_token_refresh: `pod_id` is required",
                abort_policy="abort_tool", retryable=False,
                details={"arguments": args},
            )

        # ---- read pod + verify it's an auth.oauth ---------------------

        from app.assistant.pod_store.pod_store import PodStore

        store = PodStore()
        pod = store.get(pod_id)
        if pod is None:
            return make_tool_error(
                error_code="pod_not_found",
                message=f"oauth_token_refresh: pod {pod_id!r} not found",
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id},
            )
        if pod.kind != "auth.oauth":
            return make_tool_error(
                error_code="invalid_pod_kind",
                message=(
                    f"oauth_token_refresh: pod {pod_id!r} has kind {pod.kind!r}, "
                    f"expected 'auth.oauth'"
                ),
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id, "kind": pod.kind},
            )

        meta = pod.metadata or {}
        refresh_url = meta.get("refresh_url")
        access_token_env = meta.get("env_ref_root")  # set by put_secret_pod
        refresh_token_env = meta.get("refresh_token_env_ref")
        client_id_env = meta.get("client_id_env_ref")
        client_secret_env = meta.get("client_secret_env_ref")
        provider = meta.get("provider", "unknown")

        for k, v in [
            ("refresh_url", refresh_url),
            ("env_ref_root (access_token env var)", access_token_env),
            ("refresh_token_env_ref", refresh_token_env),
        ]:
            if not v:
                return make_tool_error(
                    error_code="pod_misconfigured",
                    message=(
                        f"oauth_token_refresh: pod {pod_id!r} missing required "
                        f"metadata field {k!r}"
                    ),
                    abort_policy="abort_tool", retryable=False,
                    details={"pod_id": pod_id, "missing": k},
                )

        # ---- short-circuit on near-expiry ------------------------------

        if not force:
            seconds_remaining = self._seconds_until_expiry(pod_id, store)
            if seconds_remaining is not None and seconds_remaining > _NEAR_EXPIRY_SECONDS:
                return ToolResult(
                    result_type="oauth_token_refresh",
                    content=(
                        f"oauth_token_refresh: provider={provider!r}, refresh skipped — "
                        f"current token has ~{seconds_remaining}s remaining "
                        f"(threshold {_NEAR_EXPIRY_SECONDS}s; pass force=true to override)"
                    ),
                    data={
                        "ok": True,
                        "provider": provider,
                        "refreshed": False,
                        "seconds_remaining": seconds_remaining,
                    },
                )

        # ---- read refresh_token + client creds at courier scope --------

        try:
            refresh_token = self._fetch_at_courier(store, pod_id, "refresh_token", tool_message)
        except _CourierFetchError as e:
            return make_tool_error(
                error_code=e.error_code,
                message=f"oauth_token_refresh: {e}",
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": pod_id, "projection": "refresh_token"},
            )

        client_id = os.environ.get(client_id_env) if client_id_env else None
        client_secret = os.environ.get(client_secret_env) if client_secret_env else None

        # ---- POST to refresh endpoint ----------------------------------

        post_body: Dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if client_id:
            post_body["client_id"] = client_id
        if client_secret:
            post_body["client_secret"] = client_secret

        t0 = time.time()
        try:
            resp = requests.post(
                refresh_url,
                data=post_body,
                timeout=_REFRESH_TIMEOUT_S,
                headers={"Accept": "application/json"},
            )
        except requests.exceptions.Timeout:
            return make_tool_error(
                error_code="refresh_timeout",
                message=f"oauth_token_refresh: provider {provider!r} timed out",
                abort_policy="abort_tool", retryable=True,
                details={"refresh_url": refresh_url},
            )
        except requests.exceptions.RequestException as e:
            return make_tool_error(
                error_code="refresh_network_error",
                message=f"oauth_token_refresh: network error - {type(e).__name__}: {e}",
                abort_policy="abort_tool", retryable=True,
                details={"refresh_url": refresh_url},
            )
        finally:
            # Release the local refresh_token reference promptly.
            refresh_token = None
            del refresh_token

        duration_ms = (time.time() - t0) * 1000

        if resp.status_code != 200:
            return make_tool_error(
                error_code=f"refresh_http_{resp.status_code}",
                message=(
                    f"oauth_token_refresh: provider {provider!r} returned "
                    f"{resp.status_code}: {resp.text[:300]}"
                ),
                abort_policy="abort_tool",
                retryable=(500 <= resp.status_code < 600),
                details={"status_code": resp.status_code, "provider": provider},
            )

        try:
            body = resp.json()
        except Exception as e:
            return make_tool_error(
                error_code="refresh_response_invalid",
                message=f"oauth_token_refresh: could not parse JSON response: {e}",
                abort_policy="abort_tool", retryable=False,
                details={"body_preview": resp.text[:200]},
            )

        new_access_token = body.get("access_token")
        if not new_access_token:
            return make_tool_error(
                error_code="refresh_response_invalid",
                message=(
                    f"oauth_token_refresh: provider {provider!r} response had no "
                    f"access_token field. Body: {resp.text[:200]}"
                ),
                abort_policy="abort_tool", retryable=False,
                details={"provider": provider},
            )

        expires_in = int(body.get("expires_in", 3600))
        new_refresh_token = body.get("refresh_token")  # may be None (no rotation)

        # ---- write new values back to env vars + pod metadata ---------

        os.environ[access_token_env] = new_access_token
        refresh_rotated = False
        if new_refresh_token and new_refresh_token != os.environ.get(refresh_token_env):
            os.environ[refresh_token_env] = new_refresh_token
            refresh_rotated = True

        new_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        new_expiry_iso = new_expiry.isoformat()
        self._update_expiry_projection(store, pod_id, new_expiry_iso)
        self._update_pod_metadata_expiry(store, pod_id, new_expiry_iso)

        # Release locals.
        new_access_token = None
        new_refresh_token = None
        del new_access_token, new_refresh_token

        return ToolResult(
            result_type="oauth_token_refresh",
            content=(
                f"oauth_token_refresh: provider={provider!r} refreshed; "
                f"new expiry {new_expiry_iso} ({expires_in}s); "
                f"refresh_token_rotated={refresh_rotated}; took {duration_ms:.0f}ms"
            ),
            data={
                "ok": True,
                "provider": provider,
                "refreshed": True,
                "new_expiry_iso": new_expiry_iso,
                "expires_in_seconds": expires_in,
                "refresh_token_rotated": refresh_rotated,
                "duration_ms": duration_ms,
            },
        )

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _seconds_until_expiry(pod_id: str, store) -> Optional[int]:
        """Read the expiry projection at chat-tier scope and parse it.
        Returns None if not parseable (e.g., 'unknown')."""
        chat_scope = ScopeContext(
            scope_id="scope::oauth_refresh::expiry_check",
            owner_id="jukka", actor_id="oauth_token_refresh",
            surface="system", room_id="oauth_token_refresh",
            approval=ScopeApprovalPolicy(authority_level=50),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
            tools=ScopeToolPolicy(),
        )
        try:
            expiry_str = store.fetch_projection(pod_id, "expiry", scope=chat_scope)
        except Exception:
            return None
        try:
            expiry_dt = datetime.fromisoformat(str(expiry_str).replace("Z", "+00:00"))
        except Exception:
            return None
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
        delta = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
        return int(delta)

    @staticmethod
    def _fetch_at_courier(
        store, pod_id: str, projection: str, tool_message: ToolMessage,
    ) -> str:
        from app.assistant.pod_store.authority import PodAuthorityError
        from app.assistant.pod_store.resolvers import PodValueMissing

        scope = ScopeContext(
            scope_id="scope::oauth_token_refresh::courier",
            owner_id="jukka",
            actor_id=f"oauth_token_refresh:request_id={getattr(tool_message, 'request_id', '?')}",
            surface="system", room_id="oauth_token_refresh",
            approval=ScopeApprovalPolicy(authority_level=100),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
            tools=ScopeToolPolicy(),
        )
        try:
            value = store.fetch_projection(pod_id, projection, scope=scope)
        except PodAuthorityError as e:
            raise _CourierFetchError("pod_authority_denied", str(e))
        except KeyError as e:
            raise _CourierFetchError("pod_not_found", str(e))
        except PodValueMissing as e:
            raise _CourierFetchError("pod_value_missing", str(e))
        if not isinstance(value, str):
            try:
                value = value.decode("utf-8")
            except Exception:
                raise _CourierFetchError(
                    "pod_value_not_decodable",
                    f"pod {pod_id} projection {projection!r} returned non-string value",
                )
        return value

    @staticmethod
    def _update_expiry_projection(store, pod_id: str, new_expiry_iso: str) -> None:
        """Update the chat-tier expiry projection in pod_projection."""
        from app.assistant.pod_store.models import PodProjection
        from app.models.base import get_session

        session = get_session()
        try:
            row = (
                session.query(PodProjection)
                .filter(
                    PodProjection.pod_id == pod_id,
                    PodProjection.projection_name == "expiry",
                )
                .first()
            )
            if row is not None:
                row.plain_value = new_expiry_iso
                session.add(row)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _update_pod_metadata_expiry(store, pod_id: str, new_expiry_iso: str) -> None:
        """Update the pod's metadata_json.expiry_iso for visibility in queries."""
        from app.assistant.pod_store.models import PodRow
        from app.models.base import get_session

        session = get_session()
        try:
            row = session.query(PodRow).filter_by(pod_id=pod_id).first()
            if row is not None:
                meta = dict(row.metadata_json or {})
                meta["expiry_iso"] = new_expiry_iso
                row.metadata_json = meta
                session.add(row)
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class _CourierFetchError(Exception):
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(message)


def get_tool_class():
    return OauthTokenRefresh
