"""Pydantic agent_form for http_request.

A pod-aware HTTP tool. Auth headers and bodies can be `datapod:` references
that get resolved at courier scope; the LLM-visible args never carry the
resolved values. Responses can optionally be sealed into a new pod with a
declared privacy class (response_pod_kind).

Pod-reference encoding:
    datapod:<kind>:<id>            implicit projection 'full'
    datapod:<kind>:<id>/<proj>     explicit projection

NOTE on `from __future__ import annotations`: do NOT add it here. PEP 563
turns nested model references into strings; OpenAI's `parse` API calls
`model_json_schema()` at request time, which fails to resolve the string
type back to the actual class (Pydantic 2 + OpenAI SDK incompatibility).
Other tool_forms files in this repo follow the same convention — keep
type hints concrete.
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, field_validator


HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


class http_request_args(BaseModel):
    url: str
    method: HttpMethod = "GET"
    # Headers: dict whose VALUES can be `datapod:` references. Pod refs are
    # resolved at courier scope at execute time; the agent never sees the
    # resolved string. Use this for Authorization / X-API-Key / etc.
    headers: Optional[Dict[str, str]] = None
    # Body: ALWAYS a string. For JSON APIs the planner passes a JSON-encoded
    # string (e.g. '{"identifier":"...","password":"datapod:auth.bearer:.../full"}');
    # the tool json.loads it at execute time, recurses to resolve any
    # `datapod:` refs inside fields under courier scope, and sends as JSON.
    # For raw text/binary bodies, pass the literal string. For sealed
    # payloads, pass a single `datapod:<kind>:<id>/<projection>` reference
    # — the whole body comes from the pod.
    #
    # NOT Union[str, dict, ...] — open-ended dicts are incompatible with
    # OpenAI structured-output strict mode (see
    # feedback_planner_action_is_str_tools_validate).
    body: Optional[str] = None
    # Query params: dict[str, str] on the wire (URL encoded). Planners
    # naturally emit ints/floats/bools for things like `limit=25` — coerce
    # to string with a pre-validator so fast_validate succeeds. Otherwise
    # the args-agent LLM path engages with a Dict[str, str] schema, the
    # OpenAI strict-mode parser fails to find a clean output shape, and
    # gpt-4.1 has been observed running away to 32K+ chars before timeout.
    query_params: Optional[Dict[str, str]] = None
    timeout_s: float = 30.0
    # If set, the response body is sealed into a NEW pod with this privacy
    # class. The tool returns response_pod_id to the agent instead of the
    # body content. Use for sensitive data (health, financial, medical) that
    # the agent should be able to *cause to be fetched* without *reading*.
    response_pod_kind: Optional[str] = None
    # If set, named TOP-LEVEL fields in the response JSON are sealed into
    # pods and replaced with `datapod:` ref strings before the body reaches
    # the agent. Use when a service returns credentials (accessJwt /
    # refreshJwt / OAuth bearer / signed tokens) that subsequent calls need
    # to relay verbatim — without that relay going through LLM
    # transcription, where long opaque strings get corrupted by one or two
    # characters silently. Same architectural pattern as outbound
    # `datapod:auth.bearer:.../full` substitution in headers/body, just
    # closing the inbound side. Each sealed field becomes its own pod;
    # the response JSON the agent sees has those fields replaced with
    # `datapod:<seal_pod_kind>:<id>/full`. Default kind: "auth.session".
    seal_fields: Optional[List[str]] = None
    seal_pod_kind: Optional[str] = None
    follow_redirects: bool = True
    # If set, the tool fails if the response status is not in this list.
    expect_status: Optional[List[int]] = None

    @field_validator("query_params", mode="before")
    @classmethod
    def _stringify_query_param_values(cls, v: Any) -> Any:
        # Planners emit URL query params with their natural Python types
        # (limit=25, since_ts=1716700000, include_replies=True). On the wire
        # they all become strings. Coerce here so fast_validate accepts the
        # planner's dict directly and the args-agent LLM path doesn't engage.
        if isinstance(v, dict):
            return {str(k): str(val) if val is not None else "" for k, val in v.items()}
        return v


class http_request_arguments(BaseModel):
    tool_name: str
    arguments: http_request_args
