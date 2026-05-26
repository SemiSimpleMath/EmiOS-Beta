"""http_request — pod-aware HTTP/REST tool.

The foundational HTTP capability. Headers and body may carry `datapod:`
references; the tool elevates to courier scope, resolves them, and substitutes
the real values into the outbound request. The agent's transcript never
contains the resolved secret values. Every call writes a row to `http_audit`.

## Pod-reference encoding

In header VALUES or as a whole-body string:

    datapod:<kind>:<id>            -> projection 'full'
    datapod:<kind>:<id>/<proj>     -> projection <proj>

## What v1.2 does NOT yet support

- OAuth refresh — separate sidecar tool will handle this in v1.3.
- Cookie jar / stateful session — use `playwright_manager` instead.
- Binary response sealing — `response_pod_kind` sealing currently inlines
  the response text into the new pod's body. Binary responses (images, PDFs)
  fall back to a metadata-only pod with a placeholder body; raw bytes are
  not stored. File-backed projections for binary come later.

## response_pod_kind (sealed responses)

Set `response_pod_kind="health.private"` (or similar) to seal the response
into a new pod. The agent receives `response_pod_id` instead of the body
content. Authority for the new pod is derived from the kind suffix:

  *.private   → AUTH_USER (99)     Jukka-equivalent only
  *.gated     → AUTH_GATED (70)    sensitive-but-shareable
  *.chat      → AUTH_CHAT (50)     normal agent surfaces
  *.protected → AUTH_CHAT (50)     alias
  *.public    → AUTH_PUBLIC (10)   display-anywhere
  (other)     → AUTH_USER (99)     fail-closed default

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
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.pod_store.authority import (
    AUTH_PUBLIC, AUTH_CHAT, AUTH_GATED, AUTH_USER,
)
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
# Same shape, no anchors, no whitespace allowed inside — used to find pod-refs
# EMBEDDED inside a longer string. Critical for `Authorization: Bearer <ref>`
# style headers where the ref is preceded by a scheme keyword. The kind/id/
# projection segments forbid whitespace so the regex terminates cleanly at
# the next space/quote/end-of-value.
_POD_REF_SUBSTRING_RE = re.compile(
    r"datapod:[^:\s]+:[^/\s]+(?:/[^/\s]+)?"
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
        seal_fields_raw = args.get("seal_fields")
        seal_fields: List[str] = [
            str(f).strip() for f in (seal_fields_raw or []) if isinstance(f, str) and str(f).strip()
        ]
        seal_pod_kind = (args.get("seal_pod_kind") or "auth.session").strip() or "auth.session"
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
        # response_pod_kind is now supported (v1.1) — see _seal_response_pod
        # below for the minting path.

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

        host = urlparse(url).netloc
        soft_cap_hit = response_bytes > _SOFT_CAP_BYTES

        # ---- seal_fields: extract named top-level fields into pods, rewrite body -----
        # Closes the inbound side of the credential-confinement pattern.
        # Services like Bluesky's createSession return tokens (accessJwt /
        # refreshJwt) that subsequent calls need to relay verbatim in
        # headers. Today's failure mode: planner copies the JWT from the
        # transcript into the next call's Authorization header, LLM
        # transcription silently corrupts ~1 char in ~200, signature
        # verification fails, call returns InvalidToken. Sealing each named
        # field into its own pod and replacing it in the body with the
        # pod-ref means the JWT never enters LLM token prediction.
        sealed_field_pod_ids: List[str] = []
        if seal_fields and isinstance(body_json, dict):
            try:
                body_json, body_text, sealed_field_pod_ids = self._seal_response_fields(
                    body_json=body_json,
                    fields=seal_fields,
                    seal_pod_kind=seal_pod_kind,
                    method=method, url=url, host=host,
                    status_code=response.status_code,
                    caller_agent=self._caller_agent(tool_message),
                    request_id=getattr(tool_message, "request_id", None),
                )
                pods_used.extend(sealed_field_pod_ids)
            except Exception as e:
                logger.error("http_request: seal_fields failed: %s", e)
                logger.debug("seal_fields exception details", exc_info=True)
                # Don't fail the call — the response is still valid, the
                # planner just won't have pod-refs for the fields. Better
                # than aborting on a sealing bug.

        # ---- response_pod_kind: seal the response into a new pod -------

        response_pod_id: Optional[str] = None
        sealed_min_authority: Optional[int] = None
        if response_pod_kind:
            try:
                response_pod_id, sealed_min_authority = self._seal_response_pod(
                    response_pod_kind=response_pod_kind,
                    body_text=body_text,
                    is_text=is_text,
                    content_type=content_type,
                    method=method, url=url, host=host,
                    status_code=response.status_code,
                    response_bytes=response_bytes,
                    caller_agent=self._caller_agent(tool_message),
                    request_id=getattr(tool_message, "request_id", None),
                )
            except Exception as e:
                logger.error("http_request: response pod sealing failed: %s", e)
                logger.debug("seal_response_pod exception details", exc_info=True)
                self._write_audit(
                    request_id=getattr(tool_message, "request_id", None),
                    caller_agent=self._caller_agent(tool_message),
                    method=method, url=url,
                    status_code=response.status_code,
                    request_bytes=None, response_bytes=response_bytes,
                    pods_used=pods_used, response_pod_id=None,
                    response_pod_kind=response_pod_kind,
                    error_code="response_pod_seal_failed", duration_ms=duration_ms,
                )
                return make_tool_error(
                    error_code="response_pod_seal_failed",
                    message=f"http_request: failed to seal response into pod: {e}",
                    abort_policy="abort_tool", retryable=False,
                    details={"response_pod_kind": response_pod_kind},
                )

        self._write_audit(
            request_id=getattr(tool_message, "request_id", None),
            caller_agent=self._caller_agent(tool_message),
            method=method, url=url,
            status_code=response.status_code,
            request_bytes=None, response_bytes=response_bytes,
            pods_used=pods_used, response_pod_id=response_pod_id,
            response_pod_kind=response_pod_kind,
            error_code=None, duration_ms=duration_ms,
        )

        parts = [
            f"http_request {method} {host}:",
            f"- status: {response.status_code}",
            f"- content_type: {content_type or '(none)'}",
            f"- response_bytes: {response_bytes}",
            f"- duration_ms: {duration_ms:.0f}",
        ]
        if pods_used:
            parts.append(f"- pods_used: {len(pods_used)} (auth resolution applied)")
        if response_pod_id:
            parts.append(
                f"- response_pod_id: {response_pod_id} "
                f"(kind={response_pod_kind!r}, min_authority={sealed_min_authority})"
            )
            parts.append(
                "- response body sealed — agents at lower authority cannot read it"
            )
        elif soft_cap_hit:
            parts.append(
                f"- WARNING: response exceeds soft cap ({_SOFT_CAP_BYTES} bytes); "
                f"consider routing through a summarizer agent before downstream use."
            )
        elif is_text and isinstance(body_text, str):
            # Inline the response body so the calling planner can act on
            # values inside it (e.g. accessJwt from createSession, post
            # URIs from getTimeline, pagination cursors, JSON fields).
            # No size cap here — the soft_cap branch above already filters
            # responses > _SOFT_CAP_BYTES (256KB). Anything that lands
            # here is ≤256KB; the manager's summary_pre_node handles
            # compaction above its trigger_on_large_result_chars threshold
            # before the planner sees the prompt.
            parts.append("- body:")
            parts.append(body_text)

        result_data: Dict[str, Any] = {
            "ok": True,
            "status": response.status_code,
            "headers": dict(response.headers),
            "content_type": content_type,
            "content_length": response_bytes,
            "duration_ms": duration_ms,
            "soft_cap_hit": soft_cap_hit,
            "pods_used_count": len(pods_used),
        }
        if response_pod_id:
            # SEALED: body never exposed to the agent. Caller must
            # pod_fetch at the appropriate authority to read.
            result_data["response_pod_id"] = response_pod_id
            result_data["response_pod_kind"] = response_pod_kind
            result_data["response_pod_min_authority"] = sealed_min_authority
            # Release local refs so the body is not visible past this point.
            body_text = None
            body_json = None
        else:
            result_data["body"] = body_text
            result_data["body_json"] = body_json

        return ToolResult(
            result_type="http_request",
            content="\n".join(parts),
            data=result_data,
        )

    # ------------------------------------------------------------- sealing

    @staticmethod
    def _kind_to_min_authority(kind: str) -> int:
        """Map a response_pod_kind string to an authority band.

        Convention: the suffix after the final '.' declares the privacy class.
        Unknown suffixes fail closed at AUTH_USER (99) — the most restrictive
        non-courier band.
        """
        if not kind:
            return AUTH_USER
        suffix = kind.rsplit(".", 1)[-1].lower()
        return {
            "public":    AUTH_PUBLIC,
            "chat":      AUTH_CHAT,
            "protected": AUTH_CHAT,
            "gated":     AUTH_GATED,
            "private":   AUTH_USER,
        }.get(suffix, AUTH_USER)

    def _seal_response_fields(
        self,
        *,
        body_json: Dict[str, Any],
        fields: List[str],
        seal_pod_kind: str,
        method: str, url: str, host: str,
        status_code: int,
        caller_agent: Optional[str],
        request_id: Optional[str],
    ) -> Tuple[Dict[str, Any], str, List[str]]:
        """Extract named top-level fields from `body_json`, mint a pod per field
        carrying the raw value, and replace each field in the JSON with a
        `datapod:<seal_pod_kind>:<uuid>/full` reference string.

        Returns the rewritten body_json dict, the re-serialized body_text
        (JSON pretty-printed), and the list of pod_ids minted.

        Missing fields are silently skipped — same shape as `read_tool_result`,
        tolerant of optional fields. Non-string values are JSON-stringified
        before sealing (so an int/float/dict field would still be capturable,
        but the typical case is opaque-string tokens).

        Authority: pod kind is hashed through _kind_to_min_authority; for
        the default `auth.session` kind (no .private/.gated/.chat/.public
        suffix) this falls through to AUTH_USER (99), matching `auth.bearer`.
        """
        from app.assistant.pod_store.contracts import Pod
        from app.assistant.pod_store.pod_store import PodStore

        rewritten = dict(body_json)
        minted_pod_ids: List[str] = []
        store = PodStore()
        min_authority = self._kind_to_min_authority(seal_pod_kind)
        now_iso = datetime.now(timezone.utc).isoformat()

        for field_name in fields:
            if field_name not in rewritten:
                continue
            raw_value = rewritten[field_name]
            if raw_value is None:
                continue
            # Normalize to string for storage. The expected case is an
            # opaque token (str). Non-strings (e.g. an object payload) get
            # JSON-serialized so the sealed pod always has a string body.
            if isinstance(raw_value, str):
                stored_body = raw_value
            else:
                try:
                    stored_body = json.dumps(raw_value, ensure_ascii=False)
                except Exception:
                    stored_body = str(raw_value)

            pod_uuid = uuid.uuid4().hex
            pod_id = f"datapod:{seal_pod_kind}:{pod_uuid}"
            one_liner = (
                f"{field_name} from {method} {host} → {status_code} "
                f"(sealed at {now_iso})"
            )
            pod = Pod(
                pod_id=pod_id,
                kind=seal_pod_kind,
                tags=["http_response_field", host, seal_pod_kind, field_name],
                one_liner=one_liner,
                body=stored_body,
                source_refs=[],
                for_agents=[],
                scope_id=None,
                created_by=caller_agent or "http_request",
                metadata={
                    "url": url,
                    "method": method,
                    "host": host,
                    "status_code": status_code,
                    "sealed_field": field_name,
                    "sealed_at": now_iso,
                    "sealed_by_request_id": request_id,
                },
                min_authority=min_authority,
            )
            store.put(pod)
            minted_pod_ids.append(pod_id)
            # Replace the value with the pod-ref. /full projection matches
            # the auth.bearer pattern; resolvers already handle the "no
            # explicit projection ⇒ full" case so either form works.
            rewritten[field_name] = f"{pod_id}/full"

        rewritten_body_text = json.dumps(rewritten, ensure_ascii=False)
        return rewritten, rewritten_body_text, minted_pod_ids

    def _seal_response_pod(
        self,
        *,
        response_pod_kind: str,
        body_text: str,
        is_text: bool,
        content_type: str,
        method: str, url: str, host: str,
        status_code: int,
        response_bytes: int,
        caller_agent: Optional[str],
        request_id: Optional[str],
    ) -> Tuple[str, int]:
        """Mint a new pod holding the response body. Returns (pod_id, min_authority).

        For text responses, the body is stored inline. For binary responses,
        v1.1 stores a placeholder body (raw bytes are NOT persisted — a
        file-backed projection comes later). The pod_id format is
        `datapod:<kind>:<uuid>` so the caller can reference it via the same
        `datapod:` URI convention as identity pods.
        """
        from app.assistant.pod_store.contracts import Pod
        from app.assistant.pod_store.pod_store import PodStore

        pod_uuid = uuid.uuid4().hex
        pod_id = f"datapod:{response_pod_kind}:{pod_uuid}"
        min_authority = self._kind_to_min_authority(response_pod_kind)

        # Body: inline for text, placeholder for binary. The placeholder
        # records enough metadata for the agent to know what's there without
        # exposing the bytes.
        if is_text:
            stored_body = body_text
        else:
            stored_body = (
                f"<binary {response_bytes} bytes, content-type={content_type!r}; "
                f"raw bytes not persisted in v1.1>"
            )

        one_liner = (
            f"HTTP response: {method} {host} → {status_code} "
            f"({response_bytes} bytes, {content_type or 'no content-type'})"
        )

        pod = Pod(
            pod_id=pod_id,
            kind=response_pod_kind,
            tags=["http_response", host, response_pod_kind],
            one_liner=one_liner,
            body=stored_body,
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by=caller_agent or "http_request",
            metadata={
                "url": url,
                "method": method,
                "host": host,
                "status_code": status_code,
                "content_type": content_type,
                "content_length": response_bytes,
                "is_text": is_text,
                "sealed_at": datetime.now(timezone.utc).isoformat(),
                "sealed_by_request_id": request_id,
            },
            min_authority=min_authority,
        )
        PodStore().put(pod)
        return pod_id, min_authority

    # ----------------------------------------------------------------- pods

    def _resolve_headers(
        self,
        headers_arg: Optional[Dict[str, str]],
        pods_used: List[str],
        tool_message: ToolMessage,
    ) -> Dict[str, str]:
        """Resolve pod-refs inside header values.

        Two shapes need to work:
          1. Whole-value pod-ref:  `X-API-Key: datapod:auth.bearer:.../full`
          2. Embedded pod-ref:     `Authorization: Bearer datapod:auth.session:.../full`

        Shape (2) is the convention for bearer tokens — the scheme keyword
        sits in front of the credential. The earlier `startswith` check only
        caught (1) and let (2) fall through as a literal string, which
        Bluesky / OAuth / Stripe etc. then reject as an invalid token.
        Substring substitution covers both.
        """
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
            out[k] = self._substitute_pod_refs_in_string(v, pods_used, tool_message)
        return out

    def _substitute_pod_refs_in_string(
        self,
        value: str,
        pods_used: List[str],
        tool_message: ToolMessage,
    ) -> str:
        """Find every `datapod:<kind>:<id>(/projection)?` occurrence in `value`
        and replace it with the resolved pod content. Fast path: skip the
        regex entirely if the prefix doesn't appear at all.
        """
        if _POD_PREFIX not in value:
            return value

        def _replace(m: re.Match) -> str:
            ref = m.group(0)
            resolved = self._resolve_pod_ref(ref, tool_message)
            pods_used.append(ref)
            return resolved

        return _POD_REF_SUBSTRING_RE.sub(_replace, value)

    def _resolve_body(
        self,
        body_arg: Any,
        pods_used: List[str],
        tool_message: ToolMessage,
    ) -> Tuple[Any, Optional[str]]:
        """Returns (resolved_body, content_type_hint).

        body_arg is always a string (the schema enforces it). Three shapes:
          - Whole body is a `datapod:` pod ref → resolve to a single
            string (returned as the request body verbatim).
          - JSON-looking string (starts with `{` or `[`) → json.loads,
            recursively substitute any string field whose value starts
            with `datapod:`, return the dict/list (execute() will send
            it via requests' json= kwarg).
          - Plain text string → pass through unchanged (execute() sends
            via requests' data= kwarg).
        """
        if body_arg is None:
            return None, None
        if not isinstance(body_arg, str):
            # Schema enforces str; defense-in-depth in case a future code
            # path passes a dict directly.
            if isinstance(body_arg, (dict, list)):
                return self._resolve_pod_refs_in_container(body_arg, pods_used, tool_message), None
            return body_arg, None
        if body_arg.startswith(_POD_PREFIX):
            resolved = self._resolve_pod_ref(body_arg, tool_message)
            pods_used.append(body_arg)
            return resolved, None
        stripped = body_arg.lstrip()
        if stripped[:1] in ("{", "["):
            try:
                parsed = json.loads(body_arg)
            except json.JSONDecodeError:
                # Looks like JSON but isn't — treat as plain text.
                return body_arg, None
            if isinstance(parsed, (dict, list)):
                return self._resolve_pod_refs_in_container(parsed, pods_used, tool_message), None
            # Scalar JSON (string, number, bool, null) — pass the parsed
            # form so execute() uses json= kwarg consistently.
            return parsed, None
        return body_arg, None

    def _resolve_pod_refs_in_container(
        self,
        value: Any,
        pods_used: List[str],
        tool_message: ToolMessage,
    ) -> Any:
        """Recursively substitute pod refs inside dict + list bodies.

        Any string value starting with `datapod:` is resolved via the same
        courier-scope path as headers + whole-body refs. Non-string scalars
        (int, bool, None, float) pass through.
        """
        if isinstance(value, str):
            if value.startswith(_POD_PREFIX):
                resolved = self._resolve_pod_ref(value, tool_message)
                pods_used.append(value)
                return resolved
            return value
        if isinstance(value, dict):
            return {
                k: self._resolve_pod_refs_in_container(v, pods_used, tool_message)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [
                self._resolve_pod_refs_in_container(v, pods_used, tool_message)
                for v in value
            ]
        return value

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
