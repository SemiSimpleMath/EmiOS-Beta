"""launch_assistant_browser.py

Open the assistant's dedicated Chrome profile (data/playwright_profile) in a
NORMAL, headed, interactive Chrome window so YOU can sign in BY HAND — as the
assistant (her own gmail / reddit), never your own account.

Whatever you log into here persists in that profile and is reused automatically
by the agent: playwright-mcp launches the SAME profile (--user-data-dir) with
--browser=chrome, so cookies/sessions carry straight over. The agent's browser
is automation-controlled (you can't hand-drive it) and runs this same profile —
so THIS window is your only place to manage her login.

Run it from the IDE (run button), or:
    .venv\\Scripts\\python.exe launch_assistant_browser.py
Optionally seed a different URL (e.g. her gmail):
    .venv\\Scripts\\python.exe launch_assistant_browser.py https://accounts.google.com/

IMPORTANT: Chrome allows only ONE process per profile directory at a time. Don't
run this while the agent is driving the browser (stop the agent task first), and
close this window before running an agent task.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# This script lives at the repo root. The profile dir MUST stay equal to the MCP
# config's --user-data-dir (mcp/servers/npm/playwright-mcp.yaml ->
# ${REPO_ROOT}/data/playwright_profile) or the hand-seeded login won't reach the agent.
_REPO_ROOT = Path(__file__).resolve().parent
PROFILE_DIR = _REPO_ROOT / "data" / "playwright_profile"

DEFAULT_URL = "https://www.reddit.com/login/"

# Real Chrome (stable channel), not Chromium — genuine fingerprint. Usual install spots.
_CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / "AppData/Local/Google/Chrome/Application/chrome.exe",
]


def _find_chrome() -> Path:
    for c in _CHROME_CANDIDATES:
        if c.exists():
            return c
    raise SystemExit(
        "Google Chrome not found in the usual locations:\n  "
        + "\n  ".join(str(c) for c in _CHROME_CANDIDATES)
        + "\nInstall Chrome, or edit _CHROME_CANDIDATES in this script."
    )


def _profile_in_use() -> bool:
    """True if a chrome.exe is already bound to this profile (the agent, or a window
    you forgot to close) — a second process would fail on the lock or corrupt state.
    Best-effort: if psutil isn't importable, skip the check (Chrome errors on a lock anyway)."""
    try:
        import psutil
    except ImportError:
        print("(note) psutil unavailable — skipping the profile-lock check; make sure no "
              "other Chrome is using this profile.")
        return False
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower() == "chrome.exe":
                if "playwright_profile" in " ".join(p.info["cmdline"] or []):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    chrome = _find_chrome()

    if _profile_in_use():
        raise SystemExit(
            "A Chrome process is already using this profile. Close it (and stop any running "
            "agent task), then re-run."
        )

    PROFILE_DIR.parent.mkdir(parents=True, exist_ok=True)

    # Open on the portrait (vertical) monitor if there is one, maximized. Detection
    # is per-machine (the util), so this works on any layout; --start-maximized fills
    # whatever monitor we land on. Best-effort: if detection/import fails, Chrome just
    # opens maximized on the default monitor.
    try:
        from app.assistant.utils.monitor_utils import vertical_monitor
        mon = vertical_monitor()
    except Exception:
        mon = None
    # --force-device-scale-factor=1 makes Chrome read --window-size in the physical
    # pixels we detect; a scaled profile otherwise treats it as CSS px and overflows.
    if mon:
        mx, my, mw, mh = mon
        win_args = [f"--window-position={mx},{my}", f"--window-size={mw},{mh}",
                    "--force-device-scale-factor=1"]
        monitor_line = f"Monitor : vertical {mw}x{mh} @ ({mx},{my}) — filling it"
    else:
        win_args = ["--start-maximized"]
        monitor_line = "Monitor : no portrait monitor detected — maximized on the default monitor"

    print(f"Chrome  : {chrome}")
    print(f"Profile : {PROFILE_DIR}")
    print(monitor_line)
    print(f"Opening : {url}")
    print("Sign in as the assistant (HER own gmail / reddit) by hand, then CLOSE the window. "
          "The session persists in the profile.")
    print("IMPORTANT: do NOT sign in as yourself. If Reddit offers 'Continue with Google', "
          "pick the assistant's gmail — never your own.")

    # --no-first-run / --no-default-browser-check suppress one-time prompts. Launch detached
    # so this script returns immediately (nice from the IDE) and Chrome stays open.
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(chrome), f"--user-data-dir={PROFILE_DIR}",
         "--no-first-run", "--no-default-browser-check", *win_args, url],
        close_fds=True,
        creationflags=creationflags,
    )


if __name__ == "__main__":
    main()
