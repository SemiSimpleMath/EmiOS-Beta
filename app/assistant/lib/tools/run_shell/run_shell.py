"""Tightly-scoped shell execution tool. Hello-world v1.

Layered defenses (in order):
  1. The contract's `approval_min_authority: 99` — only Jukka-origin
     messages can invoke this through the existing tool-approval gate.
     Inbound email / SMS / dayflow-autonomous calls never reach here.
  2. Command structural denylist — no shell metacharacters that could
     chain commands or redirect output.
  3. Executable allowlist — only read-only inspection tools.
  4. Path scope — every path referenced by the command must be inside
     the configured allow_dirs. The EmiAi repo path is denied even if
     it sits under home.
  5. Timeout + output cap — 30s, 4 KB.
  6. Audit — every invocation (allowed or refused) is logged with the
     full command + reason + verdict.

This is intentionally narrow. The plan is: see what reads on this
foundation feel limiting, then widen one dimension at a time
(more commands → more paths → write commands with critic → network
commands with critic).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ---------- v1 policy ----------
ALLOWED_EXECUTABLES = {
    "ls", "find", "cat", "head", "tail", "grep", "wc", "pwd",
}


# Map shell folder display name → (registry value name, default subdir under HOME).
# OneDrive can redirect any of these; the registry tells us where they really are.
_KNOWN_FOLDERS = {
    "Documents": ("Personal", "Documents"),
    "Pictures":  ("My Pictures", "Pictures"),
    "Music":     ("My Music", "Music"),
    "Videos":    ("My Video", "Videos"),  # registry key is singular "Video"
}


def _known_folder(name: str) -> Path:
    """Return the user's real path for a known shell folder (Documents, Pictures, ...).

    On Windows the `User Shell Folders` registry key is the authoritative
    source — OneDrive updates these values when it takes over a folder.
    Falls back to `Path.home() / <default>` on non-Windows or registry miss.
    """
    reg_value, default_subdir = _KNOWN_FOLDERS[name]
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, reg_value)
                return Path(os.path.expandvars(value)).resolve()
        except OSError as e:
            logger.warning("[run_shell] Could not read %s from registry (%s); falling back to ~/%s",
                           name, e, default_subdir)
    return (Path.home() / default_subdir).resolve()


def _documents_dir() -> Path:
    """Backwards-compat shim — prefer _known_folder('Documents')."""
    return _known_folder("Documents")

# Shell metacharacters that allow command chaining, redirects, or
# substitution. Any of these in the raw command string → refuse.
FORBIDDEN_TOKENS = ("|", ">", "<", ";", "&", "`", "$(",  ")", "$(", "&&", "||")

# Commands that, even if individually invoked, are off-limits in v1.
# (Defense in depth — the allowlist already excludes these, but listing
# them explicitly makes refusal messages clearer.)
FORBIDDEN_EXECUTABLES = {
    "rm", "mv", "cp", "dd", "sudo", "su", "chmod", "chown",
    "curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet",
    "python", "python3", "node", "bash", "sh", "zsh", "powershell",
    "kill", "killall", "pkill",
}


def _allowed_dirs() -> list[Path]:
    # All four can be OneDrive-redirected — _known_folder reads the registry.
    return [_known_folder(name) for name in _KNOWN_FOLDERS]


def _denied_dirs() -> list[Path]:
    """Paths that are off-limits even if they sit under an allowed dir."""
    return [get_repo_root().resolve()]


def _path_is_inside(p: Path, parent: Path) -> bool:
    try:
        p.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _looks_like_path(token: str) -> bool:
    """Heuristic: is this argument a path the user means literally?"""
    if not token:
        return False
    return token.startswith(("/", "~", ".")) or "/" in token or "\\" in token


def _expand_path(token: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(token))).resolve()


def _validate_command(raw: str) -> tuple[bool, str]:
    """Return (ok, reason_if_refused). Defense-in-depth checks."""
    cmd = (raw or "").strip()
    if not cmd:
        return False, "Empty command."

    # Structural denylist — done on the raw string before tokenization.
    for tok in FORBIDDEN_TOKENS:
        if tok in cmd:
            return False, f"Refused: command contains forbidden token {tok!r}. Pipes, redirects, chaining, and substitution are not allowed in v1."

    try:
        parts = shlex.split(cmd, posix=True)
    except ValueError as e:
        return False, f"Refused: command failed to tokenize ({e})."
    if not parts:
        return False, "Empty command after tokenization."

    exe = Path(parts[0]).name.lower()
    if exe in FORBIDDEN_EXECUTABLES:
        return False, f"Refused: executable {exe!r} is on the v1 denylist."
    if exe not in ALLOWED_EXECUTABLES:
        return False, (
            f"Refused: executable {exe!r} is not in the v1 allowlist. "
            f"Allowed: {sorted(ALLOWED_EXECUTABLES)}."
        )

    # Path scope: every argument that looks like a path must resolve
    # inside an allowed dir AND not inside a denied dir.
    allow = _allowed_dirs()
    deny = _denied_dirs()
    for tok in parts[1:]:
        if not _looks_like_path(tok):
            continue
        if ".." in Path(tok).parts:
            return False, f"Refused: path {tok!r} contains `..` traversal."
        try:
            resolved = _expand_path(tok)
        except Exception:
            return False, f"Refused: could not resolve path {tok!r}."
        if not any(_path_is_inside(resolved, d) for d in allow):
            return False, (
                f"Refused: path {tok!r} (resolved to {resolved}) is outside "
                f"the v1 allowed dirs ({[str(d) for d in allow]})."
            )
        for d in deny:
            if _path_is_inside(resolved, d):
                return False, f"Refused: path {tok!r} is inside the denied EmiAi repo path."

    return True, ""


def _find_bash() -> str | None:
    """Locate a bash interpreter, preferring Git Bash on Windows.

    `shutil.which("bash")` is unreliable on Windows: under IntelliJ-launched
    Flask the cmd-shaped PATH finds `C:\\Windows\\System32\\bash.exe` (the WSL
    launcher) before Git Bash. WSL bash sees a different filesystem
    (`/mnt/c/...`) and our path scope checks fall apart.

    Probe the canonical Git Bash install locations first. Only fall back to
    `shutil.which` if no known Git Bash exists, AND skip any candidate under
    `\\Windows\\` or `\\WindowsApps\\` (those are the WSL trampoline).
    """
    if os.name == "nt":
        candidates = [
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        ]
        for c in candidates:
            if c and Path(c).is_file():
                return c
        # Last-resort PATH lookup, excluding the WSL trampoline.
        found = shutil.which("bash")
        if found and "\\windows\\" not in found.lower() and "\\windowsapps\\" not in found.lower():
            return found
        return None
    # Non-Windows: trust PATH.
    return shutil.which("bash")


def _home_msys_form() -> str:
    """Path.home() converted to the MSYS POSIX form bash always accepts.

    `C:\\Users\\semis` → `/c/Users/semis`. This form works on every Git
    Bash / MSYS2 / Cygwin variant; the Windows-drive-letter form
    (`C:/Users/...`) is accepted by some bash builds but not all.
    """
    p = Path.home()
    posix = p.as_posix()  # 'C:/Users/semis'
    if len(posix) >= 2 and posix[1] == ":":
        drive = posix[0].lower()
        return f"/{drive}{posix[2:]}"  # '/c/Users/semis'
    return posix


def _windows_path_to_msys(p: Path) -> str:
    """`C:\\Users\\semis\\OneDrive\\Documents` → `/c/Users/semis/OneDrive/Documents`."""
    posix = p.as_posix()
    if len(posix) >= 2 and posix[1] == ":":
        drive = posix[0].lower()
        return f"/{drive}{posix[2:]}"
    return posix


def _expand_home_in_command(command: str) -> str:
    """Replace `~` and `~/...` in the command string with absolute MSYS POSIX paths.

    Sidesteps bash's HOME resolution entirely — Git Bash on Windows reads
    /etc/profile and resets HOME to a non-existent /home/<user> POSIX path,
    which makes `~/Documents` fail even when we pass our own HOME via env.
    Doing the expansion in Python first means bash sees an absolute path.

    `~/Documents` (and any subpath) is rewritten to the user's REAL Documents
    folder (which on Windows is usually OneDrive-redirected, not the legacy
    local `~/Documents`). This makes `ls ~/Documents` Just Work for a planner
    that doesn't know about OneDrive redirection.

    Uses simple regex substitution (NOT shlex tokenize+requote) because
    re-quoting wraps glob characters like `*` in single quotes, which
    suppresses bash's glob expansion.

    Replaces `~` only where it stands as a path-start token: at the
    beginning of the string, after whitespace, or after `=`. A literal
    `~` mid-token (e.g. inside an already-quoted string) is left alone.
    """
    home_msys = _home_msys_form()
    # Each known folder may be OneDrive-redirected; rewrite `~/<Folder>` →
    # the real (possibly OneDrive) path before the bare-`~` → home rule fires.
    for name in _KNOWN_FOLDERS:
        target_msys = _windows_path_to_msys(_known_folder(name))
        # Lookbehind ensures `~/Documentsbackup` is NOT matched as `~/Documents`.
        pattern = re.compile(rf"(?<![^\s=])~/{re.escape(name)}(?=/|\s|$)")
        command = pattern.sub(target_msys, command)
    home_pattern = re.compile(r"(?<![^\s=])~(?=/|\s|$)")
    return home_pattern.sub(home_msys, command)


def _audit(command: str, reason: str, verdict: str, detail: str = "") -> None:
    """Append-only audit log. v1 just logs at INFO; later swap for a DB row."""
    logger.info(
        "[run_shell:audit] verdict=%s command=%r reason=%r detail=%s",
        verdict, command, reason, detail,
    )


class RunShellTool(BaseTool):
    def __init__(self):
        super().__init__("run_shell")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data if isinstance(tool_message.tool_data, dict) else {}
        args = tool_data.get("arguments") if isinstance(tool_data.get("arguments"), dict) else tool_data
        command = args.get("command") if isinstance(args, dict) else None
        reason = args.get("reason") if isinstance(args, dict) else None

        if not isinstance(command, str) or not command.strip():
            return ToolResult(result_type="error", content="Missing required argument: command")
        if not isinstance(reason, str) or not reason.strip():
            return ToolResult(result_type="error", content="Missing required argument: reason (one-line intent for audit log)")

        ok, refusal = _validate_command(command)
        if not ok:
            _audit(command, reason, verdict="refused", detail=refusal)
            return ToolResult(
                result_type="run_shell_refused",
                content=refusal,
                data={"ok": False, "command": command, "refused_reason": refusal},
            )

        # Pre-expand `~` in Python before bash sees the command, so Git
        # Bash's broken HOME doesn't matter.
        try:
            command_expanded = _expand_home_in_command(command)
        except Exception:
            command_expanded = command
        _audit(command_expanded, reason, verdict="allowed")
        # Always invoke via bash explicitly. Use the Windows-aware finder so
        # we land on Git Bash, not the WSL trampoline at System32\bash.exe.
        bash = _find_bash()
        if not bash:
            return ToolResult(
                result_type="run_shell_error",
                content="bash not found. run_shell requires Git Bash on Windows (C:\\Program Files\\Git\\bin\\bash.exe) or /bin/bash on Unix.",
                data={"ok": False, "command": command},
            )
        logger.info(
            "[run_shell:diag] bash=%s home_posix=%s user=%s expanded=%r",
            bash,
            Path.home().as_posix(),
            os.environ.get("USERNAME") or os.environ.get("USER") or "?",
            command_expanded,
        )
        # Force HOME to Path.home() in POSIX form so bash's `~` resolves
        # to the user's real home regardless of what HOME is in the
        # parent process. On Windows under Flask, HOME often points to
        # `/home/<user>` (an MSYS-shaped path that doesn't exist on the
        # real filesystem), which makes `~/Documents` fail. as_posix()
        # gives `C:/Users/semis` which Git Bash handles cleanly.
        env = os.environ.copy()
        env["HOME"] = _home_msys_form()
        try:
            # --noprofile --norc: do NOT load /etc/profile or ~/.bash_profile.
            # Git Bash's profile resets HOME from USERPROFILE / hardcodes
            # /home/<user>, which silently clobbered our env["HOME"]
            # override and made `~/Documents` resolve to a non-existent
            # MSYS POSIX path. With these flags bash uses our env as-is.
            proc = subprocess.run(
                [bash, "--noprofile", "--norc", "-c", command_expanded],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(Path.home()),
                env=env,
            )
        except subprocess.TimeoutExpired:
            _audit(command, reason, verdict="timeout")
            return ToolResult(
                result_type="run_shell_timeout",
                content=f"Command timed out after 30s: {command}",
                data={"ok": False, "command": command, "exit_code": None, "stdout": "", "stderr": "(timeout)"},
            )
        except Exception as e:
            _audit(command, reason, verdict="exception", detail=str(e))
            return ToolResult(
                result_type="run_shell_error",
                content=f"Failed to execute: {e}",
                data={"ok": False, "command": command, "exit_code": None, "stdout": "", "stderr": str(e)},
            )

        cap = 4096
        stdout = (proc.stdout or "")[:cap]
        stderr = (proc.stderr or "")[:cap]
        truncated = (len(proc.stdout or "") > cap) or (len(proc.stderr or "") > cap)
        # Show both the original and the bash-actual command so failure
        # diagnosis is easy. The planner sees `~/Documents`; we want it
        # to know what bash actually got.
        body_parts = [f"$ {command}"]
        if command_expanded != command:
            body_parts.append(f"(bash actually ran: {command_expanded})")
        body_parts.append(f"exit_code: {proc.returncode}")
        if stdout: body_parts.append(f"--- stdout ---\n{stdout}")
        if stderr: body_parts.append(f"--- stderr ---\n{stderr}")
        if truncated: body_parts.append("(output truncated to 4 KB)")

        return ToolResult(
            result_type="run_shell_result",
            content="\n".join(body_parts),
            data={
                "ok": proc.returncode == 0,
                "command": command,
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": truncated,
            },
        )


def get_tool_class():
    return RunShellTool
