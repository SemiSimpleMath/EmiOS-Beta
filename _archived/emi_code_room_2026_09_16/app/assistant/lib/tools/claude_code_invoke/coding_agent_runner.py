"""Run the local Claude Code CLI as a subprocess and parse its stream-json output.

EmiCode shells out to the user's installed `claude` CLI rather than calling
the Anthropic API directly — Max-subscription users have unlimited usage on
the CLI but pay per-token on the API. The CLI is invoked with
``--print --output-format stream-json`` so we get a structured event stream
that lets us extract the session_id (for ``--resume``-based multi-turn) and
the final assistant text.

Read-only by default: the ``--allowed-tools`` flag is set to a hardcoded
read-only set (Read, Glob, Grep). EmiCode's whole purpose is to surface
proposals back to the user; the user applies changes manually outside this
room.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


DEFAULT_CLI_PATH = "claude"
DEFAULT_ALLOWED_TOOLS = "Read,Glob,Grep"
DEFAULT_TIMEOUT_SECONDS = 300


@dataclass
class CodingAgentRunResult:
    """Structured result of one Claude Code CLI invocation."""

    success: bool
    final_text: str = ""
    session_id: Optional[str] = None
    duration_s: float = 0.0
    error: str = ""
    raw_events: List[dict] = field(default_factory=list)
    cli_path: str = ""
    cli_args: List[str] = field(default_factory=list)


def find_cli(cli_path: str = DEFAULT_CLI_PATH) -> Optional[str]:
    """Resolve the CLI binary, returning its full path or None if missing.

    On Windows ``claude`` is typically installed as a ``.cmd`` shim under
    ``AppData\\Roaming\\npm`` (npm-install) or similar; ``shutil.which``
    handles the PATHEXT lookup.
    """
    return shutil.which(cli_path)


def run(
    *,
    prompt: str,
    cli_path: str = DEFAULT_CLI_PATH,
    cwd: Optional[Path] = None,
    session_id: Optional[str] = None,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> CodingAgentRunResult:
    """Invoke Claude Code with the given prompt, return parsed result.

    ``prompt`` is passed via stdin (avoids command-line length limits and
    quoting headaches across shells). The CLI is invoked with ``--print``
    (one-shot, no TTY) and ``--output-format stream-json`` so we can pull
    session_id out of the system/init event.

    When ``session_id`` is provided, the CLI is invoked with ``--resume
    <session_id>`` to continue an existing conversation. The returned
    ``session_id`` will be the same id if the resume succeeded.
    """
    import time

    bin_path = find_cli(cli_path)
    if bin_path is None:
        return CodingAgentRunResult(
            success=False,
            error=(
                f"Claude Code CLI not found at {cli_path!r}. Install it "
                f"(see https://docs.claude.com/claude-code) or set "
                f"user_settings.emi_code.cli_path to the full path."
            ),
            cli_path=cli_path,
        )

    args: List[str] = [
        bin_path,
        "--print",
        "--output-format", "stream-json",
        "--verbose",  # required by Claude Code when using stream-json
        "--allowed-tools", allowed_tools,
    ]
    if session_id:
        args += ["--resume", session_id]

    cwd_path = str(cwd) if cwd else None
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            cwd=cwd_path,
        )
    except subprocess.TimeoutExpired as exc:
        return CodingAgentRunResult(
            success=False,
            error=f"Claude Code CLI timed out after {timeout_seconds}s.",
            duration_s=time.perf_counter() - started,
            cli_path=bin_path,
            cli_args=args,
        )
    except FileNotFoundError as exc:
        return CodingAgentRunResult(
            success=False,
            error=f"Failed to spawn CLI: {exc}",
            cli_path=bin_path,
            cli_args=args,
        )
    elapsed = time.perf_counter() - started

    if proc.returncode != 0:
        # CLI exited non-zero — surface stderr, plus whatever events we got.
        stderr_tail = (proc.stderr or "").strip()[-2000:]
        events, _, _ = _parse_stream(proc.stdout or "")
        return CodingAgentRunResult(
            success=False,
            error=f"CLI exit code {proc.returncode}. stderr: {stderr_tail}",
            duration_s=elapsed,
            raw_events=events,
            cli_path=bin_path,
            cli_args=args,
        )

    events, final_text, parsed_session_id = _parse_stream(proc.stdout or "")
    return CodingAgentRunResult(
        success=True,
        final_text=final_text,
        session_id=parsed_session_id or session_id,
        duration_s=elapsed,
        raw_events=events,
        cli_path=bin_path,
        cli_args=args,
    )


def _parse_stream(stdout: str) -> tuple[list[dict], str, Optional[str]]:
    """Parse a stream-json transcript from Claude Code.

    Returns ``(events, final_text, session_id)``. Each line of stdout is a
    JSON object — defensive: bad lines are logged and skipped, never raise.

    The format Claude Code emits for ``--print --output-format stream-json``:
      - First event is ``{"type": "system", "subtype": "init", "session_id": ...}``
      - Then a stream of ``assistant``/``user`` events as the conversation runs
      - Last event is ``{"type": "result", "result": "<final text>", ...}``

    We pick the final ``result.result`` if present; otherwise fall back to
    the last assistant text-content block.
    """
    events: list[dict] = []
    session_id: Optional[str] = None
    final_text = ""
    last_assistant_text = ""

    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("[claude_code_invoke] non-JSON line skipped: %r", line[:200])
            continue
        events.append(ev)

        et = str(ev.get("type") or "")
        if et == "system" and not session_id:
            sid = str(ev.get("session_id") or "").strip()
            if sid:
                session_id = sid
        elif et == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content")
            if isinstance(content, list):
                texts = [
                    str(b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if texts:
                    last_assistant_text = "\n".join(t for t in texts if t)
        elif et == "result":
            r = ev.get("result")
            if isinstance(r, str) and r.strip():
                final_text = r

    if not final_text:
        final_text = last_assistant_text
    return events, final_text, session_id
