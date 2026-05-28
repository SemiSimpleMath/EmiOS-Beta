"""execute_code — Docker-sandboxed Python execution for Emi agents.

Agents pass a script source + optional input pods. The tool:
  1. Resolves input pods at courier scope, writes their bytes to a per-call
     workspace under data/sandbox_workspaces/<call_id>/inputs/.
  2. Writes the script to workspace/script.py.
  3. Optionally pip-installs `requirements` into a layered scratch image
     (slow path).
  4. Runs `docker run` with resource limits + network policy + workspace
     bind-mount.
  5. Captures stdout/stderr; mints any files under workspace/outputs/ as
     output pods returned in the result.
  6. Optionally seals stdout into a response pod with declared privacy class.
  7. Writes a sandbox_audit row with sizes + duration + error code; never
     logs full stdout/stderr (could carry secrets).

Authority: requires AUTH_USER (99). Sandbox is high-trust; user-initiated
or user-approved only.

What v1 does NOT yet do:
- Per-domain egress enforcement. v1 sets --network=none when egress_allowlist
  is empty, --network=bridge with full network access (audit-logged) when
  not empty. v1.1 will route egress through a Squid proxy with allow-list.
- Persistent kernels (Jupyter-style session reuse). Each call is a fresh
  container.
- GPU passthrough.
- Language: Python only. Node/TS may come in v2.

See `docker/sandbox/Dockerfile` for the image; build it once with
`docker build -t emi-sandbox:v1 docker/sandbox/`.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    ToolResult,
)

logger = get_logger(__name__)


# Resource limits. Conservative defaults; not configurable per call in v1.
_DEFAULT_TIMEOUT_S = 30.0
_HARD_TIMEOUT_S = 300.0
_CPUS = "2.0"
_MEMORY = "2g"
_PIDS_LIMIT = 512
_TMPFS_SIZE = "512m"

# Output caps. stdout > soft cap triggers a "consider sealing in a pod"
# note in the result; > hard cap is truncated with an explicit marker.
_STDOUT_SOFT_CAP = 256 * 1024
_STDOUT_HARD_CAP = 2 * 1024 * 1024
_STDERR_PREVIEW_CHARS = 256

# Image name. Bumping the tag = new image build expected.
_IMAGE = "emi-sandbox:v1"

# Where per-call workspaces live on the host. Mounted into the container at
# /workspace. Pruned daily by a routine (workspaces older than 24h).
_REPO_ROOT = Path(__file__).resolve().parents[5]
_WORKSPACES_ROOT = _REPO_ROOT / "data" / "sandbox_workspaces"

# Where binary outputs are persisted so output pods can carry a real file
# (referenced via metadata.stored_path, the same convention image/document/
# audio/video pods use). Survives the per-call workspace teardown; swept by
# the sandbox_outputs_sweep routine.
_OUTPUTS_ROOT = _REPO_ROOT / "data" / "sandbox_outputs"


class ExecuteCode(BaseTool):
    """Run a Python script in a Docker sandbox."""

    requires_approval = False
    # Authority is enforced at the tool-contract layer (approval_min_authority: 99).
    # The pod-resolution courier scope is internal.

    def __init__(self):
        super().__init__("execute_code")
        self._ensure_audit_table()
        _WORKSPACES_ROOT.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ setup

    def _ensure_audit_table(self) -> None:
        try:
            from app.assistant.lib.tools.execute_code.models import SandboxAudit
            from app.models.base import Base, get_session

            session = get_session()
            try:
                engine = session.bind
                Base.metadata.create_all(
                    engine, tables=[SandboxAudit.__table__], checkfirst=True,
                )
            finally:
                session.close()
        except Exception as e:
            logger.error("execute_code: failed to ensure sandbox_audit table: %s", e)
            logger.debug("ensure_audit_table exception details", exc_info=True)

    # ----------------------------------------------------------------- execute

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        args = (tool_message.tool_data or {}).get("arguments") or {}

        source = args.get("source") or ""
        language = (args.get("language") or "python").strip().lower()
        input_pod_ids = list(args.get("input_pod_ids") or [])
        fs_allowlist = list(args.get("fs_allowlist") or [])
        egress_allowlist = list(args.get("egress_allowlist") or [])
        timeout_s = float(args.get("timeout_s") or _DEFAULT_TIMEOUT_S)
        response_pod_kind = (args.get("response_pod_kind") or "").strip() or None
        requirements = list(args.get("requirements") or [])

        request_id = getattr(tool_message, "request_id", None)
        caller_agent = self._caller_agent(tool_message)

        # ---- validate args ----------------------------------------------

        if not source or not source.strip():
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code="invalid_arguments",
                message="execute_code: `source` is required",
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )
        if language != "python":
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code="unsupported_language",
                message=f"execute_code: language {language!r} not supported in v1 (python only)",
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )
        if timeout_s > _HARD_TIMEOUT_S:
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code="timeout_too_large",
                message=(
                    f"execute_code: timeout_s={timeout_s} exceeds hard cap {_HARD_TIMEOUT_S}s. "
                    f"For longer work, factor into a dayflow routine."
                ),
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )

        # ---- prepare workspace ------------------------------------------

        call_id = uuid.uuid4().hex
        workspace = _WORKSPACES_ROOT / call_id
        try:
            (workspace / "inputs").mkdir(parents=True, exist_ok=True)
            (workspace / "outputs").mkdir(parents=True, exist_ok=True)
            (workspace / "script.py").write_text(source, encoding="utf-8")
        except Exception as e:
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code="workspace_setup_failed",
                message=f"execute_code: could not prepare workspace: {e}",
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )

        # ---- resolve input pods at courier scope ------------------------

        try:
            self._stage_input_pods(input_pod_ids, workspace / "inputs", request_id)
        except _PodStageError as e:
            self._cleanup_on_failure(workspace)
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code=e.error_code,
                message=f"execute_code: {e}",
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )

        # ---- build docker command ---------------------------------------

        cmd, image_to_use = self._build_docker_cmd(
            workspace=workspace,
            fs_allowlist=fs_allowlist,
            egress_allowlist=egress_allowlist,
            timeout_s=timeout_s,
            requirements=requirements,
        )

        # ---- run --------------------------------------------------------

        t0 = time.time()
        try:
            # Hard kill at timeout_s + 5s; Docker --stop-timeout handles
            # graceful stop inside that window.
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout_s + 5.0,
                check=False,
            )
            duration_ms = (time.time() - t0) * 1000
            stdout_bytes = proc.stdout or b""
            stderr_bytes = proc.stderr or b""
            exit_code = int(proc.returncode)
            timed_out = False
        except subprocess.TimeoutExpired as e:
            duration_ms = (time.time() - t0) * 1000
            stdout_bytes = (e.stdout or b"") if hasattr(e, "stdout") else b""
            stderr_bytes = (e.stderr or b"") if hasattr(e, "stderr") else b""
            exit_code = -1
            timed_out = True
            # Best-effort kill any lingering container.
            self._kill_container_by_name(f"emi-sandbox-{call_id}")
        except FileNotFoundError:
            self._cleanup_on_failure(workspace)
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code="docker_not_installed",
                message=(
                    "execute_code: `docker` command not found. Install Docker Desktop "
                    "and ensure it's running. See docker/sandbox/README.md."
                ),
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )
        except Exception as e:
            self._cleanup_on_failure(workspace)
            return self._fail(
                request_id=request_id, caller_agent=caller_agent,
                language=language, error_code="docker_run_failed",
                message=f"execute_code: docker run exception: {type(e).__name__}: {e}",
                timeout_s=timeout_s, fs_allowlist=fs_allowlist,
                egress_allowlist=egress_allowlist, input_pod_ids=input_pod_ids,
                requirements=requirements, response_pod_kind=response_pod_kind,
            )

        # ---- decode stdout/stderr ---------------------------------------

        try:
            stdout_text = stdout_bytes.decode("utf-8", errors="replace")
        except Exception:
            stdout_text = ""
        try:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        except Exception:
            stderr_text = ""

        stdout_size = len(stdout_bytes)
        stderr_size = len(stderr_bytes)
        soft_cap_hit = stdout_size > _STDOUT_SOFT_CAP
        hard_cap_hit = stdout_size > _STDOUT_HARD_CAP
        if hard_cap_hit:
            stdout_text = stdout_text[:_STDOUT_HARD_CAP] + "\n[stdout truncated at 2MB hard cap]"

        # ---- mint output pods from workspace/outputs/ -------------------

        output_pod_ids: List[str] = []
        try:
            output_pod_ids = self._mint_output_pods(workspace / "outputs", call_id)
        except Exception as e:
            logger.error("execute_code: minting output pods failed: %s", e)
            logger.debug("mint_output_pods exception details", exc_info=True)

        # ---- optionally seal stdout into response pod -------------------

        response_pod_id: Optional[str] = None
        if response_pod_kind:
            try:
                response_pod_id = self._seal_stdout_to_pod(
                    stdout_text=stdout_text,
                    response_pod_kind=response_pod_kind,
                    caller_agent=caller_agent,
                    request_id=request_id,
                    call_id=call_id,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                )
                # Release local ref to the stdout text past this point.
                stdout_text = None
            except Exception as e:
                logger.error("execute_code: response pod seal failed: %s", e)
                logger.debug("seal_stdout exception details", exc_info=True)

        # ---- audit ------------------------------------------------------

        error_code = None
        if timed_out:
            error_code = "timeout"
        elif exit_code != 0:
            error_code = f"runtime_error_exit_{exit_code}"
        self._write_audit(
            request_id=request_id, caller_agent=caller_agent,
            language=language, source=source, duration_ms=duration_ms,
            exit_code=exit_code, timeout_s=timeout_s,
            fs_allowlist=fs_allowlist, egress_allowlist=egress_allowlist,
            input_pod_ids=input_pod_ids, output_pod_ids=output_pod_ids,
            response_pod_id=response_pod_id, response_pod_kind=response_pod_kind,
            stdout_bytes=stdout_size, stderr_bytes=stderr_size,
            stderr_preview=stderr_text[:_STDERR_PREVIEW_CHARS],
            error_code=error_code, requirements=requirements,
        )

        # ---- build ToolResult -------------------------------------------

        if timed_out:
            return make_tool_error(
                error_code="timeout",
                message=(
                    f"execute_code: timed out after {timeout_s}s. "
                    f"Consider raising timeout_s (up to {_HARD_TIMEOUT_S}s) or "
                    f"factoring the work into a dayflow routine."
                ),
                abort_policy="abort_tool", retryable=False,
                details={"timeout_s": timeout_s, "duration_ms": duration_ms},
            )

        result_data: Dict[str, Any] = {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "stdout_bytes": stdout_size,
            "stderr_bytes": stderr_size,
            "stderr": stderr_text[:_STDERR_PREVIEW_CHARS] if stderr_text else "",
            "stdout_truncated": hard_cap_hit,
            "stdout_soft_cap_hit": soft_cap_hit,
            "output_pod_ids": output_pod_ids,
            "egress_allowlist_used": egress_allowlist,
        }
        if response_pod_id:
            result_data["response_pod_id"] = response_pod_id
            result_data["response_pod_kind"] = response_pod_kind
        else:
            result_data["stdout"] = stdout_text

        parts = [
            f"execute_code:",
            f"- exit_code: {exit_code}",
            f"- duration_ms: {duration_ms:.0f}",
            f"- stdout_bytes: {stdout_size}",
            f"- stderr_bytes: {stderr_size}",
        ]
        if output_pod_ids:
            parts.append(f"- output_pod_ids: {len(output_pod_ids)} file(s) minted")
        if response_pod_id:
            parts.append(f"- response_pod_id: {response_pod_id} (kind={response_pod_kind!r})")
        if soft_cap_hit and not response_pod_id:
            parts.append(
                f"- WARNING: stdout > {_STDOUT_SOFT_CAP} bytes; consider response_pod_kind to seal it."
            )
        if exit_code != 0:
            parts.append(f"- stderr preview: {stderr_text[:200]!r}")

        return ToolResult(
            result_type="execute_code",
            content="\n".join(parts),
            data=result_data,
        )

    # -------------------------------------------------------------- helpers

    def _build_docker_cmd(
        self,
        *,
        workspace: Path,
        fs_allowlist: List[str],
        egress_allowlist: List[str],
        timeout_s: float,
        requirements: List[str],
    ) -> Tuple[List[str], str]:
        """Build the `docker run` argv. Returns (cmd_list, image_used)."""
        call_id = workspace.name
        container_name = f"emi-sandbox-{call_id}"

        cmd = [
            "docker", "run",
            "--rm",
            "--name", container_name,
            f"--cpus={_CPUS}",
            f"--memory={_MEMORY}",
            f"--pids-limit={_PIDS_LIMIT}",
            "--read-only",
            f"--tmpfs=/tmp:size={_TMPFS_SIZE}",
            "--stop-timeout", "3",
        ]

        # Network: --network=none when no egress_allowlist; otherwise default
        # bridge (full network access, audited). v1.1 will route via proxy.
        if not egress_allowlist:
            cmd += ["--network=none"]
        # else: default bridge network (no flag needed)

        # Bind-mount workspace at /workspace (read-write).
        # Docker on Windows needs Windows-style absolute paths; convert as needed.
        ws_host = str(workspace.resolve())
        cmd += ["-v", f"{ws_host}:/workspace"]

        # fs_allowlist: read-only mounts.
        for host_path in fs_allowlist:
            try:
                hp = Path(host_path).expanduser().resolve()
                if not hp.exists():
                    logger.warning("execute_code: fs_allowlist path missing: %s", host_path)
                    continue
                cmd += ["-v", f"{str(hp)}:/workspace/host_{hp.name}:ro"]
            except Exception as e:
                logger.warning("execute_code: fs_allowlist path invalid %r: %s", host_path, e)

        # If requirements were declared, install them into a layered image-on-the-fly
        # would be ideal. v1 simplification: run the install inline before script.py.
        if requirements:
            # Replace the default entrypoint with a shell that pip-installs then runs.
            install_args = " ".join(f"{shellquote(r)}" for r in requirements)
            shell_cmd = (
                f"pip install --no-cache-dir {install_args} "
                f"&& python /workspace/script.py"
            )
            cmd += ["--entrypoint", "sh", _IMAGE, "-c", shell_cmd]
        else:
            cmd += [_IMAGE]

        return cmd, _IMAGE

    def _stage_input_pods(
        self,
        input_pod_ids: List[str],
        inputs_dir: Path,
        request_id: Optional[str],
    ) -> None:
        """Resolve each input pod at courier scope; write its bytes to inputs_dir.

        Three pod shapes are supported:
          1. Projection-style (identity, auth, artifact) — rows in PodProjection.
             Pick 'full' if visible at courier scope, else the highest
             min_authority projection the courier can read.
          2. File-backed (image, video, audio, document minted via
             mint_pod_from_path / file_ingest / image_ingest) — bytes live on
             disk at `metadata.stored_path`; `pod.body` is empty or a
             vision-caption placeholder. Read the file directly.
          3. Body-style (chat_cluster, tool_result, email, generated docs) —
             no projections, no stored_path; payload in PodRow.body.
        """
        if not input_pod_ids:
            return
        from app.assistant.pod_store.pod_store import PodStore
        from app.assistant.pod_store.authority import PodAuthorityError
        from app.assistant.pod_store.resolvers import PodValueMissing

        from app.assistant.manager_runtime.services.scope_adapter import ScopeAdapter
        courier_scope = ScopeAdapter.for_courier_call(
            tool_name="execute_code",
            request_id=request_id,
        )

        store = PodStore()
        for pod_uri in input_pod_ids:
            try:
                projections = store.list_projections(pod_uri, scope=courier_scope)
            except KeyError:
                projections = []
            except Exception as e:
                logger.debug(
                    "execute_code: list_projections for %s raised %s; falling back to body",
                    pod_uri, e,
                )
                projections = []

            value: Any = None
            ext_hint: Optional[str] = None
            if projections:
                chosen = next(
                    (p for p in projections if p["projection_name"] == "full"),
                    None,
                ) or max(projections, key=lambda p: p["min_authority"])
                try:
                    value = store.fetch_projection(
                        pod_uri, chosen["projection_name"], scope=courier_scope,
                    )
                except PodAuthorityError as e:
                    raise _PodStageError("pod_authority_denied", str(e))
                except PodValueMissing as e:
                    raise _PodStageError("pod_value_missing", str(e))
                except KeyError as e:
                    raise _PodStageError("pod_not_found", str(e))

            pod = None
            if value is None:
                pod = store.get(pod_uri)
                if pod is None:
                    raise _PodStageError(
                        "pod_not_found",
                        f"pod {pod_uri} has no projections and no body row",
                    )
                # File-backed pod: bytes on disk, body is empty or text caption.
                stored_path_str = (pod.metadata or {}).get("stored_path")
                if stored_path_str:
                    stored_path = Path(stored_path_str)
                    if not stored_path.is_absolute():
                        stored_path = _REPO_ROOT / stored_path
                    if not stored_path.is_file():
                        raise _PodStageError(
                            "pod_stored_file_missing",
                            f"pod {pod_uri} declares metadata.stored_path={stored_path_str!r} "
                            f"but file is missing at {stored_path}",
                        )
                    value = stored_path.read_bytes()
                    ext_hint = stored_path.suffix.lstrip(".") or (pod.metadata or {}).get("ext", "").lstrip(".")
                else:
                    value = pod.body or ""

            safe_name = self._pod_filename(pod_uri, ext=ext_hint)
            target = inputs_dir / safe_name
            if isinstance(value, bytes):
                target.write_bytes(value)
            elif isinstance(value, str):
                target.write_text(value, encoding="utf-8")
            else:
                raise _PodStageError(
                    "pod_value_not_writable",
                    f"pod {pod_uri} resolved to {type(value).__name__}; can't stage to disk",
                )

    @staticmethod
    def _pod_filename(pod_uri: str, ext: Optional[str] = None) -> str:
        """Map a pod URI to a safe local filename.

        Format: ``<kind>_<id_prefix>[.ext]``. The extension is appended when
        the caller knows it (file-backed pods carry ``metadata.ext`` /
        ``metadata.stored_path``'s suffix); helps downstream libs that
        sniff by extension (ffmpeg, magic-by-name).
        """
        parts = pod_uri.split(":")
        if len(parts) >= 3:
            kind = parts[1].replace(".", "_").replace("/", "_")
            ident = parts[2].replace("/", "_")
        else:
            kind = "pod"
            ident = pod_uri.replace(":", "_").replace("/", "_")
        base = f"{kind}_{ident[:32]}"
        ext = (ext or "").lstrip(".").lower()
        if ext:
            base = f"{base}.{ext}"
        return base

    def _mint_output_pods(self, outputs_dir: Path, call_id: str) -> List[str]:
        """Each file in outputs_dir becomes a new pod (kind=tool_result).

        Text-decodable files keep their content inline in pod.body. Binary
        files are copied to data/sandbox_outputs/<call_id>/<filename> and the
        pod carries metadata.stored_path so downstream tools (send_email,
        pod_fetch) attach the real file. Per-call workspace is torn down
        after this method returns; the persisted copy is what survives.
        """
        if not outputs_dir.exists():
            return []
        from app.assistant.pod_store.contracts import Pod
        from app.assistant.pod_store.pod_store import PodStore
        from app.assistant.utils.path_utils import get_repo_root

        repo_root = get_repo_root()
        pod_ids: List[str] = []
        store = PodStore()
        for f in sorted(outputs_dir.iterdir()):
            if not f.is_file():
                continue
            try:
                data = f.read_bytes()
                try:
                    body_text = data.decode("utf-8")
                    stored_rel: Optional[str] = None
                except Exception:
                    body_text = (
                        f"Binary file ({len(data)} bytes) attachable via pod_id. "
                        f"Real bytes at metadata.stored_path."
                    )
                    persist_dir = _OUTPUTS_ROOT / call_id
                    persist_dir.mkdir(parents=True, exist_ok=True)
                    persist_path = persist_dir / f.name
                    shutil.copyfile(f, persist_path)
                    stored_rel = str(persist_path.resolve().relative_to(repo_root)).replace("\\", "/")
                pod_id = f"datapod:tool_result:exec_{call_id}_{f.name}"
                metadata = {
                    "filename": f.name,
                    "bytes": len(data),
                    "call_id": call_id,
                    "minted_at": datetime.now(timezone.utc).isoformat(),
                }
                if stored_rel:
                    metadata["stored_path"] = stored_rel
                pod = Pod(
                    pod_id=pod_id,
                    kind="tool_result",
                    tags=["execute_code_output", f.suffix.lstrip(".") or "unknown"],
                    one_liner=f"execute_code output: {f.name} ({len(data)} bytes)",
                    body=body_text,
                    source_refs=[],
                    for_agents=[],
                    scope_id=None,
                    created_by="execute_code",
                    metadata=metadata,
                )
                store.put(pod)
                pod_ids.append(pod_id)
            except Exception as e:
                logger.warning("execute_code: failed to mint output pod for %s: %s", f, e)
        return pod_ids

    def _seal_stdout_to_pod(
        self,
        *,
        stdout_text: str,
        response_pod_kind: str,
        caller_agent: Optional[str],
        request_id: Optional[str],
        call_id: str,
        exit_code: int,
        duration_ms: float,
    ) -> str:
        """Mint a new pod with stdout sealed inside, authority derived from kind."""
        from app.assistant.lib.tools.http_request.http_request import HttpRequest
        from app.assistant.pod_store.contracts import Pod
        from app.assistant.pod_store.pod_store import PodStore

        # Reuse the kind→authority mapping from http_request (consistent semantics).
        min_authority = HttpRequest._kind_to_min_authority(response_pod_kind)

        pod_id = f"datapod:{response_pod_kind}:execcode_{call_id}"
        pod = Pod(
            pod_id=pod_id,
            kind=response_pod_kind,
            tags=["execute_code_response", response_pod_kind],
            one_liner=(
                f"execute_code stdout (exit={exit_code}, "
                f"{duration_ms:.0f}ms): sealed at {response_pod_kind}"
            ),
            body=stdout_text,
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by=caller_agent or "execute_code",
            metadata={
                "call_id": call_id,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "sealed_at": datetime.now(timezone.utc).isoformat(),
                "sealed_by_request_id": request_id,
            },
            min_authority=min_authority,
        )
        PodStore().put(pod)
        return pod_id

    # ----------------------------------------------------------------- audit

    def _write_audit(
        self,
        *,
        request_id: Optional[str], caller_agent: Optional[str],
        language: str, source: str, duration_ms: Optional[float],
        exit_code: Optional[int], timeout_s: float,
        fs_allowlist: List[str], egress_allowlist: List[str],
        input_pod_ids: List[str], output_pod_ids: List[str],
        response_pod_id: Optional[str], response_pod_kind: Optional[str],
        stdout_bytes: Optional[int], stderr_bytes: Optional[int],
        stderr_preview: Optional[str], error_code: Optional[str],
        requirements: List[str],
    ) -> None:
        try:
            from app.assistant.lib.tools.execute_code.models import SandboxAudit
            from app.models.base import get_session

            source_hash = hashlib.sha256((source or "").encode("utf-8")).hexdigest()
            source_bytes = len((source or "").encode("utf-8"))

            session = get_session()
            try:
                row = SandboxAudit(
                    created_at=datetime.now(timezone.utc),
                    request_id=request_id,
                    caller_agent=caller_agent,
                    language=language,
                    source_hash=source_hash,
                    source_bytes=source_bytes,
                    duration_ms=duration_ms,
                    exit_code=exit_code,
                    timeout_s=timeout_s,
                    fs_allowlist=json.dumps(fs_allowlist) if fs_allowlist else None,
                    egress_allowlist=json.dumps(egress_allowlist) if egress_allowlist else None,
                    egress_logged=None,    # v1.1: populated when proxy enforces
                    input_pod_ids=json.dumps(input_pod_ids) if input_pod_ids else None,
                    output_pod_ids=json.dumps(output_pod_ids) if output_pod_ids else None,
                    response_pod_id=response_pod_id,
                    response_pod_kind=response_pod_kind,
                    stdout_bytes=stdout_bytes,
                    stderr_bytes=stderr_bytes,
                    stderr_preview=stderr_preview,
                    error_code=error_code,
                    requirements=json.dumps(requirements) if requirements else None,
                )
                session.add(row)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        except Exception as e:
            logger.error("execute_code: failed to write sandbox_audit: %s", e)
            logger.debug("write_audit exception details", exc_info=True)

    def _fail(
        self,
        *,
        request_id: Optional[str], caller_agent: Optional[str],
        language: str, error_code: str, message: str,
        timeout_s: float, fs_allowlist: List[str], egress_allowlist: List[str],
        input_pod_ids: List[str], requirements: List[str],
        response_pod_kind: Optional[str],
    ) -> ToolResult:
        # Write an audit row for the failure path too, so we can find these later.
        self._write_audit(
            request_id=request_id, caller_agent=caller_agent,
            language=language, source="",
            duration_ms=None, exit_code=None, timeout_s=timeout_s,
            fs_allowlist=fs_allowlist, egress_allowlist=egress_allowlist,
            input_pod_ids=input_pod_ids, output_pod_ids=[],
            response_pod_id=None, response_pod_kind=response_pod_kind,
            stdout_bytes=None, stderr_bytes=None, stderr_preview=None,
            error_code=error_code, requirements=requirements,
        )
        return make_tool_error(
            error_code=error_code,
            message=message,
            abort_policy="abort_tool", retryable=False,
            details={},
        )

    @staticmethod
    def _kill_container_by_name(name: str) -> None:
        """Best-effort kill of a lingering container after timeout."""
        try:
            subprocess.run(
                ["docker", "kill", name],
                capture_output=True, timeout=10.0, check=False,
            )
        except Exception as e:
            logger.debug("execute_code: kill container %s failed (non-fatal): %s", name, e)

    @staticmethod
    def _cleanup_on_failure(workspace: Path) -> None:
        """Remove the workspace if we never even ran the container."""
        try:
            shutil.rmtree(workspace, ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def _caller_agent(tool_message: ToolMessage) -> Optional[str]:
        return (
            getattr(tool_message, "source_agent", None)
            or getattr(tool_message, "agent_name", None)
            or None
        )


class _PodStageError(Exception):
    """Failed to stage an input pod to the workspace."""
    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        super().__init__(message)


def shellquote(s: str) -> str:
    """Minimal POSIX-shell quoting for the requirements list. We're injecting
    into an `sh -c` string inside docker run; package names from pip are
    already constrained (no special chars), but defense-in-depth."""
    if not s or any(c in s for c in (" \t\n'\"\\$;&|`<>(){}[]*?")):
        return "'" + s.replace("'", "'\"'\"'") + "'"
    return s


def get_tool_class():
    return ExecuteCode
