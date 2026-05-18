"""Pydantic agent_form for http_request.

A pod-aware HTTP tool. Auth headers and bodies can be `datapod:` references
that get resolved at courier scope; the LLM-visible args never carry the
resolved values. Responses can optionally be sealed into a new pod with a
declared privacy class (response_pod_kind).

Pod-reference encoding:
    datapod:<kind>:<id>            implicit projection 'full'
    datapod:<kind>:<id>/<proj>     explicit projection
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel


HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"]


class http_request_args(BaseModel):
    url: str
    method: HttpMethod = "GET"
    # Headers: dict whose VALUES can be `datapod:` references. Pod refs are
    # resolved at courier scope at execute time; the agent never sees the
    # resolved string. Use this for Authorization / X-API-Key / etc.
    headers: Optional[Dict[str, str]] = None
    # Body: a literal string/dict/bytes OR a `datapod:` reference. If a pod
    # reference, the entire body comes from the pod (useful for file uploads
    # and sensitive payloads). Per-field substitution inside a dict body is
    # NOT supported in v1 — construct the pod up front if you need it.
    body: Optional[Union[str, Dict[str, Any], bytes]] = None
    query_params: Optional[Dict[str, str]] = None
    timeout_s: float = 30.0
    # If set, the response body is sealed into a NEW pod with this privacy
    # class. The tool returns response_pod_id to the agent instead of the
    # body content. Use for sensitive data (health, financial, medical) that
    # the agent should be able to *cause to be fetched* without *reading*.
    response_pod_kind: Optional[str] = None
    follow_redirects: bool = True
    # If set, the tool fails if the response status is not in this list.
    expect_status: Optional[List[int]] = None


class http_request_arguments(BaseModel):
    tool_name: str
    arguments: http_request_args
