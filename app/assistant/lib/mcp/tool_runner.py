from __future__ import annotations

import atexit
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.assistant.lib.mcp.stdio_client import StdioJsonRpcClient
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


class _StdioSession:
    def __init__(self, proc: subprocess.Popen, client: StdioJsonRpcClient, *, server_id: str):
        self.proc = proc
        self.client = client
        self.server_id = server_id
        self.started_at = time.time()

    def is_alive(self) -> bool:
        try:
            return self.proc.poll() is None
        except Exception:
            return False


# Simple in-process session cache for stateful MCP stdio servers (e.g., Playwright).
# Keyed by server_id.
_STDIO_SESSIONS: dict[str, _StdioSession] = {}


def _close_all_stdio_sessions() -> None:
    # Best-effort cleanup to avoid orphaned `npx @playwright/mcp` processes
    # when tests crash or exit early.
    try:
        server_ids = list(_STDIO_SESSIONS.keys())
    except Exception:
        server_ids = []
    for sid in server_ids:
        try:
            _close_stdio_session(sid)
        except Exception:
            pass


# Ensure stateful MCP server processes don't linger across test runs.
atexit.register(_close_all_stdio_sessions)


def _is_stateful_server(server_entry: dict[str, Any]) -> bool:
    """
    Some MCP servers (notably Playwright) require session continuity across tool calls.

    Emi currently spawns per-call for most servers. For Playwright, we reuse a single
    process per server_id within the host process.
    """
    try:
        return str(server_entry.get("server_id") or "") == "npm/playwright-mcp"
    except Exception:
        return False


def _start_stdio_server_process(server_entry: dict[str, Any]) -> _StdioSession:
    launch = _select_stdio_launch_option(server_entry)
    command = launch.get("command")
    cmd_args = launch.get("args")
    if not isinstance(command, str) or not isinstance(cmd_args, list):
        raise ValueError("stdio launch option must include command (str) and args (list)")

    # Resolve ${REPO_ROOT} placeholders in args so paths work cross-platform.
    from app.assistant.utils.path_utils import get_repo_root
    repo_root = str(get_repo_root())
    cmd_args = [str(a).replace("${REPO_ROOT}", repo_root) for a in cmd_args]

    # Build subprocess env. IMPORTANT: many IDEs inject PYTHONPATH to point at
    # the repo, which can shadow server dependencies (this repo contains `mcp/` pkg).
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if isinstance(launch.get("env"), dict):
        env.update(_resolve_env_placeholders(launch["env"]))

    # IMPORTANT: run MCP servers from a neutral cwd to avoid repo-local packages shadowing deps.
    cwd = launch.get("cwd") or launch.get("working_directory") or tempfile.gettempdir()

    logger.debug(f"MCP stdio launch: command={command!r} args={cmd_args!r} cwd={cwd!r}")

    proc = subprocess.Popen(
        [command, *cmd_args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd),
    )

    client = StdioJsonRpcClient(proc, timeout_s=float(server_entry.get("policy", {}).get("call_timeout_seconds", 20)))

    # Attempt initialize (some older servers may reject; ignore failures).
    try:
        init_resp = client.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "emiai", "version": "0.1"},
            },
        ).raw
        if isinstance(init_resp, dict) and "error" in init_resp:
            logger.debug(f"MCP initialize rejected (continuing): {init_resp.get('error')}")
    except Exception as e:
        logger.debug(f"MCP initialize skipped/failed (continuing): {e}")

    server_id = str(server_entry.get("server_id") or "")
    return _StdioSession(proc, client, server_id=server_id)


def _get_stdio_session(server_entry: dict[str, Any]) -> _StdioSession:
    server_id = str(server_entry.get("server_id") or "")
    if not server_id:
        # Fallback: treat as non-cacheable
        return _start_stdio_server_process(server_entry)

    sess = _STDIO_SESSIONS.get(server_id)
    if sess is not None and sess.is_alive():
        return sess

    # Start new session
    sess = _start_stdio_server_process(server_entry)
    _STDIO_SESSIONS[server_id] = sess
    return sess


def _close_stdio_session(server_id: str) -> None:
    sess = _STDIO_SESSIONS.pop(server_id, None)
    if not sess:
        return
    try:
        sess.proc.terminate()
    except Exception:
        pass
    try:
        sess.proc.wait(timeout=2)
    except Exception:
        try:
            sess.proc.kill()
        except Exception:
            pass


def _terminate_process(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _call_resp_text(call_resp: dict[str, Any]) -> str:
    """
    Best-effort extraction of text content from an MCP tools/call response.
    """
    result = call_resp.get("result")
    if not isinstance(result, dict):
        return ""
    content_items = result.get("content", [])
    if not isinstance(content_items, list):
        return ""
    parts: list[str] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def _maybe_cleanup_playwright_user_data_dir(error_text: str) -> bool:
    """
    Playwright launch failures can be caused by a locked/corrupt user-data-dir for the
    persistent context. If we can detect it in the error text, delete it.

    Returns True if we attempted deletion.
    """
    if not error_text:
        return False

    patterns = [
        r'--user-data-dir="([^"]+)"',
        r"--user-data-dir=([^\s]+)",
    ]

    for pat in patterns:
        m = re.search(pat, error_text)
        if not m:
            continue
        path = (m.group(1) or "").strip().strip('"')
        if not path:
            continue
        lower = path.lower()
        # Only delete temp-like dirs for safety.
        if "\\appdata\\local\\temp" not in lower and "\\temp" not in lower and "/tmp" not in lower:
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            logger.warning("Playwright MCP: deleted user-data-dir after launch failure: %s", path)
        except Exception as e:
            logger.debug("Playwright MCP: failed to delete user-data-dir %s: %s", path, e, exc_info=True)
        return True

    return False

def _uploads_temp_dir() -> Path:
    # Mirror existing UI upload flow: uploads/temp/
    from app.assistant.utils.path_utils import get_uploads_dir
    return get_uploads_dir() / "temp"


def _prune_temp_mcp_images(*, tmp_dir: Path, max_files: int = 200, max_age_hours: int = 24) -> None:
    """
    Best-effort retention policy for MCP-persisted images (mcp_<uuid>.<ext>).

    These can be produced at high frequency by Playwright automation.
    Keep only a bounded set to prevent disk spam.
    """
    try:
        if not tmp_dir.exists():
            return
        max_files = int(max_files) if int(max_files) > 0 else 200
        max_age_hours = int(max_age_hours) if int(max_age_hours) > 0 else 24

        now_ts = time.time()
        max_age_s = float(max_age_hours) * 3600.0

        # Only prune files we created.
        files = [p for p in tmp_dir.glob("mcp_*") if p.is_file()]
        if not files:
            return

        # Age-based pruning first.
        for p in files:
            try:
                if (now_ts - float(p.stat().st_mtime)) > max_age_s:
                    p.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("MCP image pruning: failed to remove aged file %s: %s", p, e)
                continue

        # Count-based pruning.
        files = [p for p in tmp_dir.glob("mcp_*") if p.is_file()]
        if len(files) <= max_files:
            return
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            try:
                p.unlink(missing_ok=True)
            except Exception as e:
                logger.debug("MCP image pruning: failed to remove excess file %s: %s", p, e)
                continue
    except Exception as e:
        logger.debug("MCP image pruning failed: %s", e, exc_info=True)
        return


def _ext_from_mime(mime: str) -> str:
    m = (mime or "").lower().strip()
    if m == "image/png":
        return ".png"
    if m in ("image/jpeg", "image/jpg"):
        return ".jpg"
    if m == "image/webp":
        return ".webp"
    if m == "image/gif":
        return ".gif"
    return ".bin"


def _extract_mcp_image_items(call_resp: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract image-like content items from an MCP tools/call response.

    We support common shapes:
    - {"type":"image","data":"<base64>","mimeType":"image/png"}
    - {"type":"image","data":"<base64>","mime_type":"image/png"}

    Returns a list of dicts: {"mime": str, "data_b64": str}.
    """
    result = call_resp.get("result")
    if not isinstance(result, dict):
        return []
    content_items = result.get("content", [])
    if not isinstance(content_items, list):
        return []

    out: list[dict[str, Any]] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image":
            continue
        data_b64 = item.get("data")
        if not isinstance(data_b64, str) or not data_b64.strip():
            continue
        mime = item.get("mimeType") or item.get("mime_type") or item.get("mime") or "application/octet-stream"
        if not isinstance(mime, str):
            mime = "application/octet-stream"
        out.append({"mime": mime, "data_b64": data_b64.strip()})
    return out


def _persist_mcp_images(call_resp: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Persist MCP image content items to disk and return Emi-style attachment dicts.

    Attachment format mirrors UI uploads (`process_request.py`):
      {"type":"image","path": "...", "content_type": "...", "original_filename": "...", ...}
    """
    items = _extract_mcp_image_items(call_resp)
    if not items:
        return []

    tmp_dir = _uploads_temp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)

    attachments: list[dict[str, Any]] = []
    for img in items:
        mime = str(img.get("mime") or "application/octet-stream")
        data_b64 = str(img.get("data_b64") or "")
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception as e:
            logger.warning(f"Failed to decode MCP image base64 ({mime}): {e}")
            continue

        ext = _ext_from_mime(mime)
        fname = f"mcp_{uuid.uuid4().hex}{ext}"
        path = tmp_dir / fname
        try:
            path.write_bytes(raw)
        except Exception as e:
            logger.warning(f"Failed to write MCP image to disk: {path} ({e})")
            continue

        attachments.append(
            {
                "type": "image",
                "path": str(path),
                "original_filename": fname,
                "content_type": mime,
                "size_bytes": int(len(raw)),
                "source": "mcp",
            }
        )

    # Best-effort retention: prune old mcp_* files.
    try:
        _prune_temp_mcp_images(tmp_dir=tmp_dir, max_files=200, max_age_hours=24)
    except Exception as e:
        logger.debug("MCP image retention prune failed: %s", e, exc_info=True)

    return attachments


def _select_stdio_launch_option(server_entry: dict[str, Any]) -> dict[str, Any]:
    opts = server_entry.get("launch_options") or []
    if not isinstance(opts, list) or not opts:
        raise ValueError("MCP server entry must include launch_options")

    def _can_import_module(python_exe: str, module_name: str) -> tuple[bool, str]:
        try:
            proc = subprocess.run(
                [python_exe, "-c", f"import {module_name}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
            )
        except Exception as e:
            return False, f"import check failed: {e}"
        if proc.returncode == 0:
            return True, "import ok"
        stderr = (proc.stderr or "").strip()
        return False, stderr or f"import failed with exit_code={proc.returncode}"

    def _docker_daemon_ready(docker_cmd: str) -> tuple[bool, str]:
        """
        Best-effort check that Docker CLI can reach a running daemon.
        On Windows, a common failure is missing/norunning docker_engine named pipe.
        """
        try:
            proc = subprocess.run(
                [docker_cmd, "info"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3.0,
            )
        except Exception as e:
            return False, f"docker info check failed: {e}"
        if proc.returncode == 0:
            return True, "docker daemon reachable"
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        msg = stderr or stdout or f"docker info failed with exit_code={proc.returncode}"
        return False, msg

    def _resolve_command(cmd: str) -> tuple[str, bool, str]:
        # Prefer the current interpreter for python-based launch options.
        if cmd in {"python", "python3"}:
            resolved = sys.executable
            return resolved, True, "resolved to sys.executable"

        # Absolute/relative path to an executable.
        if os.path.sep in cmd or (os.path.altsep and os.path.altsep in cmd):
            if os.path.exists(cmd):
                return cmd, True, "path exists"
            return cmd, False, "path not found"

        # Search PATH.
        found = shutil.which(cmd)
        if found:
            return found, True, "found on PATH"
        return cmd, False, "not found on PATH"

    errors: list[str] = []
    for opt in opts:
        if not isinstance(opt, dict) or opt.get("transport") != "stdio":
            continue
        cmd = opt.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            errors.append(f"launch_option {opt.get('id')!r}: missing command")
            continue
        resolved_cmd, ok, why = _resolve_command(cmd.strip())
        if not ok:
            errors.append(f"launch_option {opt.get('id')!r}: command {cmd!r} unavailable ({why})")
            continue

        # If this is a docker-based launcher, ensure the daemon is reachable before selecting.
        if cmd.strip() == "docker" or resolved_cmd == "docker":
            ok_docker, why_docker = _docker_daemon_ready(cmd.strip())
            if not ok_docker:
                errors.append(f"launch_option {opt.get('id')!r}: docker daemon not ready ({why_docker})")
                continue

        # If this is a python `-m module` style launcher, only select it if the module is installed.
        # This avoids confusing "No module named ..." failures at runtime when other launch options exist.
        args = opt.get("args")
        if (
            resolved_cmd == sys.executable
            and isinstance(args, list)
            and len(args) >= 2
            and args[0] == "-m"
            and isinstance(args[1], str)
        ):
            module_name = args[1]
            ok_mod, why_mod = _can_import_module(resolved_cmd, module_name)
            if not ok_mod:
                errors.append(
                    f"launch_option {opt.get('id')!r}: python module {module_name!r} not available ({why_mod})"
                )
                continue

        # Return a copy so we can safely override python -> sys.executable.
        chosen = dict(opt)
        chosen["command"] = resolved_cmd
        return chosen

    detail = ("\n- " + "\n- ".join(errors)) if errors else ""
    raise ValueError("No usable stdio launch option found for MCP server entry." + detail)


def _resolve_env_placeholders(raw_env: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    pattern = re.compile(r"\$\{([A-Z0-9_]+)\}")
    for k, v in raw_env.items():
        key = str(k)
        val = str(v)
        matches = pattern.findall(val)
        if not matches:
            out[key] = val
            continue
        resolved = val
        for env_key in matches:
            env_val = os.environ.get(env_key)
            if env_val is None:
                raise ValueError(
                    f"Missing required environment variable '{env_key}' for MCP launch env key '{key}'."
                )
            resolved = resolved.replace(f"${{{env_key}}}", env_val)
        out[key] = resolved
    return out


def mcp_stdio_call_tool(
    *,
    server_entry: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    timeout_s: float = 20.0,
) -> dict[str, Any]:
    """
    Run a single MCP `tools/call` over stdio.

    This spawns the server process per call (simple + safe for now).
    """
    # For stateful servers (e.g., Playwright) reuse a single stdio process so
    # navigate -> screenshot -> snapshot sequences work.
    stateful = _is_stateful_server(server_entry)

    if stateful:
        sess = _get_stdio_session(server_entry)
        client = sess.client
        def _do_call() -> dict[str, Any]:
            return client.request(
                "tools/call",
                {
                    "name": tool_name,
                    "arguments": arguments or {},
                },
            ).raw

        call_resp = _do_call()
        try:
            stderr_preview = client.stderr_preview()
            if stderr_preview:
                call_resp["_emiai_stderr"] = stderr_preview
        except Exception as e:
            logger.debug("MCP stderr_preview failed (stateful): %s", e)
        # (often due to a locked/corrupt temp profile directory). Restart and retry once.
        try:
            result = call_resp.get("result")
            is_err = bool(result.get("isError", False)) if isinstance(result, dict) else False
            txt = _call_resp_text(call_resp)
            if is_err and ("launchPersistentContext" in txt) and ("Failed to launch the browser process" in txt):
                logger.warning("Playwright MCP launch failed; restarting session and retrying once.")
                _maybe_cleanup_playwright_user_data_dir(txt)
                _close_stdio_session(sess.server_id)
                sess = _get_stdio_session(server_entry)
                client = sess.client
                call_resp = _do_call()
        except Exception as e:
            logger.debug(f"Playwright MCP recovery attempt failed/skipped: {e}")

        # If the caller explicitly closes the browser, also close the server process to avoid leaks.
        if tool_name in {"browser_close"}:
            _close_stdio_session(sess.server_id)
        return call_resp

    # Default: spawn per call (safe and simple).
    sess = _start_stdio_server_process(server_entry)
    try:
        client = sess.client
        # Override per-call timeout if requested.
        client.timeout_s = timeout_s
        call_resp = client.request(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments or {},
            },
        ).raw
        try:
            stderr_preview = client.stderr_preview()
            if stderr_preview:
                call_resp["_emiai_stderr"] = stderr_preview
        except Exception as e:
            logger.debug("MCP stderr_preview failed (ephemeral): %s", e)
        return call_resp
    finally:
        _terminate_process(sess.proc)


def format_mcp_tool_result_content(call_resp: dict[str, Any]) -> tuple[str, bool, list[dict[str, Any]]]:
    """
    Convert MCP `tools/call` response to a single text content string.
    Returns (text, is_error, attachments).
    """
    result = call_resp.get("result")
    if not isinstance(result, dict):
        # If server returned protocol error shape, bubble it up as error text.
        err = call_resp.get("error")
        return (str(err) if err is not None else str(call_resp), True, [])

    is_error = bool(result.get("isError", False))
    content_items = result.get("content", [])
    parts: list[str] = []
    if isinstance(content_items, list):
        for item in content_items:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "text":
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
            elif t == "image":
                # Image content: persisted to disk; include a human marker here.
                parts.append("[image]")
            else:
                # For now, include non-text items as a short marker.
                parts.append(f"[{t}]")

    attachments = _persist_mcp_images(call_resp)
    if attachments:
        for att in attachments:
            # Include a stable marker for agents to re-use later.
            p = att.get("path")
            if isinstance(p, str) and p.strip():
                parts.append(f"[image attached: {att.get('original_filename')}]")
                parts.append(f"[mcp_image_path: {p}]")

    text = "\n\n".join(parts).strip()
    if not text:
        # Some servers may return structuredContent only; include a compact fallback.
        structured = result.get("structuredContent")
        if structured is not None:
            text = str(structured)

    # If the server logged anything on stderr, include it on errors to aid debugging.
    if is_error:
        stderr_preview = call_resp.get("_emiai_stderr")
        if isinstance(stderr_preview, str) and stderr_preview.strip():
            text = (text + "\n\n[server stderr]\n" + stderr_preview.strip()).strip()
    return (text, is_error, attachments)


def sanitize_mcp_call_response_for_history(
    call_resp: dict[str, Any],
    attachments: list[dict[str, Any]] | None = None,
    *,
    max_text_chars: int = 20000,
) -> dict[str, Any]:
    """
    Return a sanitized MCP tools/call response suitable for storing in agent history.

    Critical: do NOT retain base64 image bytes (can be megabytes and will explode prompts/logs).
    We keep only lightweight metadata and (optionally) map images to persisted file paths.
    """
    atts = [a for a in (attachments or []) if isinstance(a, dict)]
    att_paths: list[str] = []
    for a in atts:
        p = a.get("path")
        if isinstance(p, str) and p.strip():
            att_paths.append(p.strip())

    # Copy top-level keys without ever copying large nested blobs.
    out: dict[str, Any] = {}
    for k in ("jsonrpc", "id", "_emiai_stderr"):
        if k in call_resp:
            out[k] = call_resp.get(k)

    # Preserve error shape, but truncate message strings if huge.
    if isinstance(call_resp.get("error"), dict):
        err = dict(call_resp["error"])
        msg = err.get("message")
        if isinstance(msg, str) and len(msg) > max_text_chars:
            err["message"] = msg[:max_text_chars] + "…[truncated]"
        out["error"] = err

    result = call_resp.get("result")
    if not isinstance(result, dict):
        return out

    sanitized_result: dict[str, Any] = {}
    if "isError" in result:
        sanitized_result["isError"] = bool(result.get("isError", False))

    # Sanitize content list (drop image.data).
    content_items = result.get("content", [])
    sanitized_content: list[dict[str, Any]] = []
    if isinstance(content_items, list):
        img_i = 0
        for item in content_items:
            if not isinstance(item, dict):
                continue
            t = item.get("type")
            if t == "image":
                mime = item.get("mimeType") or item.get("mime_type") or item.get("mime")
                entry: dict[str, Any] = {"type": "image"}
                if isinstance(mime, str) and mime.strip():
                    entry["mimeType"] = mime.strip()
                # Map to persisted path if available.
                if img_i < len(att_paths):
                    entry["saved_path"] = att_paths[img_i]
                img_i += 1
                sanitized_content.append(entry)
                continue

            if t == "text":
                txt = item.get("text")
                if isinstance(txt, str) and len(txt) > max_text_chars:
                    txt = txt[:max_text_chars] + "…[truncated]"
                sanitized_content.append({"type": "text", "text": txt})
                continue

            # Unknown types: keep minimal fields and truncate large strings.
            entry = {"type": t}
            for kk, vv in item.items():
                if kk in {"data"}:
                    continue
                if isinstance(vv, str) and len(vv) > max_text_chars:
                    entry[kk] = vv[:max_text_chars] + "…[truncated]"
                else:
                    entry[kk] = vv
            sanitized_content.append(entry)

    if sanitized_content:
        sanitized_result["content"] = sanitized_content

    # Keep structuredContent only if it's already small (avoid giant payloads).
    sc = result.get("structuredContent")
    if sc is not None:
        try:
            sc_str = str(sc)
            if len(sc_str) <= max_text_chars:
                sanitized_result["structuredContent"] = sc
        except Exception as e:
            logger.debug("MCP structuredContent serialization failed, skipping: %s", e)

    out["result"] = sanitized_result
    return out

