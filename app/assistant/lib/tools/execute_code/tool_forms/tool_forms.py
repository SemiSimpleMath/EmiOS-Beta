"""Pydantic agent_form for execute_code.

The execute_code tool runs an agent-supplied Python script inside a Docker
sandbox. Input pods get mounted as files; files written to /workspace/outputs/
auto-mint as output pods.

NOTE: do NOT add `from __future__ import annotations` here. PEP 563
stringifies nested Pydantic refs; OpenAI's `parse` API can't resolve them at
schema codegen time. See http_request/tool_forms/tool_forms.py for the
full explanation.
"""
from typing import List, Literal, Optional

from pydantic import BaseModel


class execute_code_args(BaseModel):
    # The script source. For Python, just the body — `def main():` etc. is
    # optional. The container runs it as a standalone script.
    source: str
    # Language. Python only in v1. Node/TS may come in v2.
    language: Literal["python"] = "python"
    # Pod URIs to make available as files inside the sandbox at
    # /workspace/inputs/<pod_id>.<ext>. Pods are resolved at courier scope;
    # raw bytes never enter the agent's transcript.
    input_pod_ids: List[str] = []
    # Extra host paths that the sandbox can read (mounted read-only at
    # /workspace/host_<basename>). Use sparingly — most scripts should work
    # with input_pod_ids only.
    fs_allowlist: List[str] = []
    # Domains the script is allowed to reach. v1: advisory + audit only
    # (full enforcement is v1.1). Default empty → network is denied.
    # Example: ["api.openai.com", "fal.run"]
    egress_allowlist: List[str] = []
    # Hard timeout in seconds. Default 30s, hard cap 300s.
    timeout_s: float = 30.0
    # If set, stdout is sealed into a new pod with this privacy class.
    # The tool returns response_pod_id instead of the stdout text.
    response_pod_kind: Optional[str] = None
    # Extra pip packages to install in the sandbox BEFORE running the script.
    # Slow (~15-60s per package). Use only when a needed library isn't in
    # the base image. Example: ["faster-whisper", "weasyprint"]
    requirements: List[str] = []


class execute_code_arguments(BaseModel):
    tool_name: str
    arguments: execute_code_args
