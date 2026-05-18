"""http_request — pod-aware HTTP/REST tool.

The foundational HTTP capability. Headers and body may carry `datapod:`
references; the tool elevates to courier scope, resolves them, and substitutes
the real values into the outbound request. The agent's transcript never
contains the resolved secret values. Every call writes a row to `http_audit`.

## Pod-reference encoding

In header VALUES or as a whole-body string:

    datapod:<kind>:<id>            -> projection 'full'
    datapod:<kind>:<id>/<proj>     -> projection <proj>

## What v1 does NOT yet support

- `response_pod_kind` — sealing the response into a new pod. The contract
  documents it; setting it returns `not_implemented`. Coming in v1.1, gated
  by a real pod-minting path for response bytes (text vs binary).
- Per-field pod substitution inside a dict body. Pass a whole-body pod
  reference instead.
- OAuth refresh — separate sidecar tool will handle this in v1.1.
- Cookie jar / stateful session — use `playwright_manager` instead.

## Authority

The tool itself runs at the caller's scope authority. Pod resolution
synthesizes a fresh courier scope (authority 100) for the substitution step
only — never elevates the caller. The contract declares
`approval_min_authority: 70` (AUTH_GATED) for the tool; downstream pod
authority gates may raise that effective bar.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

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


# Pod-ref encoding: `datapod:<kind>:<id>` or `datapod:<kind>:<id>/<projection>`.
# The projection segment (`/<proj>`) is optional; defaults to 'full'.
_POD_REF_RE = re.compile(
    r"^datapod:(?P<kind>[^:]+):(?P<id>[^/]+)(?:/(?P<projection>[^/]+))?$"
)
_POD_PREFIX = "datapod:"

# Size caps. Soft cap: caller should consider summarization (we surface the
# trigger but don't auto-summarize at this layer — the manager wraps that).
# Hard cap: refuse with response_too_large.
_SOFT_CAP_BYTES = 256 * 1024
_HARD_CAP_BYTES = 2 * 1024 * 1024


class HttpRequest(BaseTool):
    """Make an HTTP request. Pod-aware for auth and body."""

    requires_approval = False  # Authority gate on pods handles sensitive auth.

    def __init__(self):
        super().__init__("http_request")
        self._ensure_audit_table()

    # ----------------------------------------------------------------- setup

    def _ensure_audit_table(self) -> None:
        """Create the http_audit table if missing. Best-effort; logged on failure."""
        try:
            from app.assistant.lib.tools.http_request.models import HttpAudit
            from app.models.base import Base, get_session

            session = get_session()
            try:
                engine = session.bind
                Base.metadata.create_all(
                    engine, tables=[HttpAudit.__table__], checkfirst=True,
                )
            finally:
                session.close()
        except Exception as e:
            logger.error("http_request: failed to ensure http_audit table: %s", e)
            logger.debug("ensure_audit_table exception details", exc_info=True)
            # Audit is non-critical — don't block the tool from instantiating.

    # ----------------------------------------------------------------- execute

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments") or {}

        url = (args.get("url") or "").strip()
        method = (args.get("method") or "GET").strip().upper()
        headers_arg = args.get("headers")
        body_arg = args.get("body")
        query_params = args.get("query_params")
        timeout_s = float(args.get("timeout_s") or 30.0)
        response_pod_kind = (args.get("response_pod_kind") or "").strip() or None
        follow_redirects = bool(args.get("follow_redirects", True))
        expect_status = args.get("expect_status")

        # ---- validate ---------------------------------------------------

        if not url:
            return make_tool_error(
                error_code="invalid_arguments",
                message="http_request error: `url` is required",
                abort_policy="abort_tool", retryable=False,
                details={"arguments": args},
            )
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            return make_tool_error(
                error_code="invalid_arguments",
                message=f"http_request error: unsupported method {method!r}",
                abort_policy="abort_tool", retryable=False,
                details={"method": method},
            )
        if response_pod_kind:
            # v1.1 territory — fresh pod minting path for response bytes.
            return make_tool_error(
                error_code="not_implemented",
                message=(
                    "http_request: response_pod_kind is documented but not "
                    "implemented in v1. Coming in v1.1. For sensitive responses "
                    "today, mint a pod manually after the call or use a courier "
                    "tool that reads the response directly."
                ),
                abort_policy="abort_tool", retryable=False,
                details={"response_pod_kind": response_pod_kind},
            )

        # ---- resolve pod refs in headers + body at courier scope --------

        pods_used: List[str] = []
        try:
            resolved_headers = self._resolve_headers(headers_arg, pods_used, tool_message)
            resolved_body, body_content_type = self._resolve_body(body_arg, pods_used, tool_message)
        except _PodResolutionError as e:
            self._write_audit(
                request_id=getattr(tool_message, "request_id", None),
                caller_agent=self._caller_agent(tool_message),
                method=method, url=url,
                status_code=None, request_bytes=None, response_bytes=None,
                pods_used=pods_used, response_pod_id=None, response_pod_kind=None,
                error_code=e.error_code, duration_ms=None,
            )
            return make_tool_error(
                error_code=e.error_code,
                message=f"http_request: {e}",
                abort_policy="abort_tool", retryable=False,
                details={"pod_id": e.pod_id, "projection": e.projection},
            )

        # ---- make the request ------------------------------------------

        t0 = time.time()
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=resolved_headers or None,
                data=resolved_body if isinstance(resolved_body, (bytes, str)) else None,
                json=resolved_body if isinstance(resolved_body, dict) else None,
                params=query_params or None,
                timeout=timeout_s,
                allow_redirects=follow_redirects,
            )
        except requests.exceptions.Timeout:
            self._write_audit(
                request_id=getattr(tool_message, "request_id", None),
                caller_agent=self._caller_agent(tool_message),
                method=method, url=url,
                status_code=None, request_bytes=None, response_bytes=None,
                pods_used=pods_used, response_pod_id=None, response_pod_kind=None,
                error_code="network_timeout",
                duration_ms=(time.time() - t0) * 1000,
            )
            return make_tool_error(
                error_code="network_timeout",
                message=f"http_request: timed out after {timeout_s}s",
                abort_policy="abort_tool", retryable=True,
                details={"url": url, "timeout_s": timeout_s},
            )
        except requests.exceptions.RequestException as e:
            self._write_audit(
                request_id=getattr(tool_message, "request_id", None),
                caller_agent=self._caller_agent(tool_message),
                method=method, url=url,
                status_code=None, request_bytes=None, response_bytes=None,
                pods_used=pods_used, response_pod_id=None, response_pod_kind=None,
                error_code="network_error",
                duration_ms=(time.time() - t0) * 1000,
            )
            return make_tool_error(
                error_code="network_error",
                message=f"http_request: network error - {type(e).__name__}: {e}",
                abort_policy="abort_tool", retryable=True,
                details={"url": url},
            )

        duration_ms = (time.time() - t0) * 1000
        response_bytes = len(response.content or b"")

        # ---- size caps --------------------------------------------------

        if response_bytes > _HARD_CAP_BYTES:
            self._write_audit(
                request_id=getattr(tool_message, "request_id", None),
                caller_agent=self._caller_agent(tool_message),
                method=method, url=url,
                status_code=response.status_code,
                request_bytes=None, response_bytes=response_bytes,
                pods_used=pods_used, response_pod_id=None, response_pod_kind=None,
                error_code="response_too_large", duration_ms=duration_ms,
            )
            return make_tool_error(
                error_code="response_too_large",
                message=(
                    f"http_request: response is {response_bytes} bytes, exceeds "
                    f"hard cap {_HARD_CAP_BYTES}. Use a different endpoint or "
                    f"pass a query that narrows results."
                ),
                abort_policy="abort_tool", retryable=False,
                details={"response_bytes": response_bytes, "hard_cap": _HARD_CAP_BYTES},
            )

        # ---- expect_status check ---------------------------------------

        if expect_status and response.status_code not in expect_status:
            self._write_audit(
                request_id=getattr(tool_message, "request_id", None),
                caller_agent=self._caller_agent(tool_message),
                method=method, url=url,
                status_code=response.status_code,
                request_bytes=None, response_bytes=response_bytes,
                pods_used=pods_used, response_pod_id=None, response_pod_kind=None,
                error_code=f"http_{response.status_code}", duration_ms=duration_ms,
            )
            preview = response.text[:500] if response.text else ""
            return make_tool_error(
                error_code=f"http_{response.status_code}",
                message=(
                    f"http_request: status {response.status_code} not in "
                    f"expected {expect_status}. Body preview: {preview!r}"
                ),
                abort_policy="abort_tool", retryable=(500 <= response.status_code < 600),
                details={"status_code": response.status_code, "expect_status": expect_status},
            )

        # ---- success: build the ToolResult -----------------------------

        # Decode body. text honors the response's Content-Type charset; we
        # fall back to a hex preview for non-text content.
        content_type = response.headers.get("Content-Type", "") or ""
        is_text = (
            content_type.startswith("text/")
            or "json" in content_type
            or "xml" in content_type
            or "javascript" in content_type
        )
        if is_text:
            body_text = response.text
        else:
            body_text = f"<binary {response_bytes} bytes; content-type={content_type!r}>"

        # Try to parse JSON for the structured `data` payload.
        body_json: Optional[Any] = None
        if "json" in content_type:
            try:
                body_json = response.json()
            except Exception:
                body_json = None

        self._write_audit(
            request_id=getattr(tool_message, "request_id", None),
            caller_agent=self._caller_agent(tool_message),
            method=method, url=url,
            status_code=response.status_code,
            request_bytes=None, response_bytes=response_bytes,
            pods_used=pods_used, response_pod_id=None, response_pod_kind=None,
            error_code=None, duration_ms=duration_ms,
        )

        host = urlparse(url).netloc
        soft_cap_hit = response_bytes > _SOFT_CAP_BYTES
        parts = [
            f"http_request {method} {host}:",
            f"- status: {response.status_code}",
            f"- content_type: {content_type or '(none)'}",
            f"- response_bytes: {response_bytes}",
            f"- duration_ms: {duration_ms:.0f}",
        ]
        if pods_used:
            parts.append(f"- pods_used: {len(pods_used)} (auth resolution applied)")
        if soft_cap_hit:
            parts.append(
                f"- WARNING: response exceeds soft cap ({_SOFT_CAP_BYTES} bytes); "
                f"consider routing through a summarizer agent before downstream use."
            )

        result_data: Dict[str, Any] = {
            "ok": True,
            "status": response.status_code,
            "headers": dict(response.headers),
            "content_type": content_type,
            "content_length": response_bytes,
            "duration_ms": duration_ms,
            "body": body_text,
            "body_json": body_json,
            "soft_cap_hit": soft_cap_hit,
            "pods_used_count": len(pods_used),
        }

        return ToolResult(
            result_type="http_request",
            content="\n".join(parts),
            data=result_data,
        )

    # ----------------------------------------------------------------- pods

    def _resolve_headers(
        self,
        headers_arg: Optional[Dict[str, str]],
        pods_used: List[str],
        tool_message: ToolMessage,
    ) -> Dict[str, str]:
        if not headers_arg:
            return {}
        if not isinstance(headers_arg, dict):
            raise _PodResolutionError(
                error_code="invalid_arguments",
                message="headers must be a dict[str, str]",
                pod_id=None, projection=None,
            )
        out: Dict[str, str] = {}
        for k, v in headers_arg.items():
            if not isinstance(v, str):
                raise _PodResolutionError(
                    error_code="invalid_arguments",
                    message=f"header value for {k!r} must be a string",
                    pod_id=None, projection=None,
                )
            if v.startswith(_POD_PREFIX):
                resolved = self._resolve_pod_ref(v, tool_message)
                pods_used.append(v)
                out[k] = resolved
            else:
                out[k] = v
        return out

    def _resolve_body(
        self,
        body_arg: Any,
        pods_used: List[str],
        tool_message: ToolMessage,
    ) -> Tuple[Any, Optional[str]]:
        """Returns (resolved_body, content_type_hint)."""
        if body_arg is None:
            return None, None
        if isinstance(body_arg, str) and body_arg.startswith(_POD_PREFIX):
            resolved = self._resolve_pod_ref(body_arg, tool_message)
            pods_used.append(body_arg)
            return resolved, None
        # Plain string / dict / bytes: pass through. requests will encode
        # dict as JSON via the json= kwarg in execute().
        return body_arg, None

    def _resolve_pod_ref(self, ref: str, tool_message: ToolMessage) -> str:
        match = _POD_REF_RE.match(ref)
        if not match:
            raise _PodResolutionError(
                error_code="invalid_pod_ref",
                message=f"malformed pod reference {ref!r}",
                pod_id=ref, projection=None,
            )
        pod_kind = match.group("kind")
        pod_id_part = match.group("id")
        projection = match.group("projection") or "full"
        full_pod_id = f"datapod:{pod_kind}:{pod_id_part}"

        # Elevate to courier scope JUST for this resolution. The agent's own
        # scope is never elevated.
        from app.assistant.pod_store.pod_store import PodStore
        from app.assistant.pod_store.authority import PodAuthorityError
        from app.assistant.pod_store.resolvers import PodValueMissing

        courier_scope = ScopeContext(
            scope_id="scope::http_request::courier",
            owner_id="jukka",
            actor_id=f"http_request:request_id={getattr(tool_message, 'request_id', '?')}",
            surface="system",
            room_id="http_request",
            approval=ScopeApprovalPolicy(authority_level=100),
            resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
            tools=ScopeToolPolicy(),
        )
        try:
            value = PodStore().fetch_projection(
                full_pod_id, projection, scope=courier_scope,
            )
        except PodAuthorityError as e:
            raise _PodResolutionError(
                error_code="pod_authority_denied",
                message=str(e),
                pod_id=full_pod_id, projection=projection,
            )
        except KeyError as e:
            raise _PodResolutionError(
                error_code="pod_not_found",
                message=str(e),
                pod_id=full_pod_id, projection=projection,
            )
        except PodValueMissing as e:
            raise _PodResolutionError(
                error_code="pod_value_missing",
                message=str(e),
                pod_id=full_pod_id, projection=projection,
            )

        if not isinstance(value, str):
            # Body refs can be bytes; header refs must be strings. The caller
            # distinguishes via header-vs-body context, but for header pods we
            # need a string. For body pods, bytes are fine.
            # We return bytes-as-bytes-decoded-latin1 here as a fallback path;
            # the caller will know if it expected text.
            try:
                value = value.decode("utf-8")
            except Exception:
                raise _PodResolutionError(
                    error_code="pod_value_not_decodable",
                    message=(
                        f"pod {full_pod_id} projection {projection!r} returned "
                        f"bytes that are not UTF-8 decodable; for binary uploads, "
                        f"use a body pod (binary upload is documented but not yet "
                        f"end-to-end tested in v1)"
                    ),
                    pod_id=full_pod_id, projection=projection,
                )
        return value

    # ----------------------------------------------------------------- audit

    def _write_audit(
        self,
        *,
        request_id: Optional[str],
        caller_agent: Optional[str],
        method: str, url: str,
        status_code: Optional[int],
        request_bytes: Optional[int],
        response_bytes: Optional[int],
        pods_used: List[str],
        response_pod_id: Optional[str],
        response_pod_kind: Optional[str],
        error_code: Optional[str],
        duration_ms: Optional[float],
    ) -> None:
        try:
            from app.assistant.lib.tools.http_request.models import HttpAudit
            from app.models.base import get_session

            parsed = urlparse(url)
            session = get_session()
            try:
                row = HttpAudit(
                    created_at=datetime.now(timezone.utc),
                    request_id=request_id,
                    caller_agent=caller_agent,
                    method=method,
                    url_host=parsed.netloc,
                    url_path=parsed.path or "/",
                    status_code=status_code,
                    request_bytes=request_bytes,
                    response_bytes=response_bytes,
                    pod_ids_used=json.dumps(pods_used) if pods_used else None,
                    response_pod_id=response_pod_id,
                    response_pod_kind=response_pod_kind,
                    error_code=error_code,
                    duration_ms=duration_ms,
                )
                session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        except Exception as e:
            # Audit failure must not block the request itself.
            logger.error("http_request: failed to write audit row: %s", e)
            logger.debug("write_audit exception details", exc_info=True)

    @staticmethod
    def _caller_agent(tool_message: ToolMessage) -> Optional[str]:
        """Best-effort extraction of the calling agent name."""
        return (
            getattr(tool_message, "source_agent", None)
            or getattr(tool_message, "agent_name", None)
            or None
        )


class _PodResolutionError(Exception):
    """Internal: pod resolution failure with structured error code for the tool result."""
    def __init__(
        self, *,
        error_code: str,
        message: str,
        pod_id: Optional[str],
        projection: Optional[str],
    ):
        self.error_code = error_code
        self.pod_id = pod_id
        self.projection = projection
        super().__init__(message)


def get_tool_class():
    return HttpRequest
