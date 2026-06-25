"""Generate the per-machine Playwright MCP browser config.

Window geometry is machine-specific — everyone's monitor layout differs — so this
is GENERATED locally from live monitor detection and written under data/
(gitignored). It is NEVER committed: the shared repo carries no one's coordinates.

@playwright/mcp loads it via `--config`. What it sets, with a portrait monitor:
- `--window-position` + `--window-size` to the portrait monitor's WORK AREA (full
  bounds minus the taskbar) so the agent's browser fills the vertical display without
  hiding under the taskbar.
- `--force-device-scale-factor=1` neutralizes the profile's render scale
  (devicePixelRatio) so Chrome reads `--window-size` in the same physical pixels we
  detect. Without it a scaled profile (e.g. dpr 1.57) treats the size as CSS pixels and
  the window overflows the monitor by that factor.
- `contextOptions.viewport: null` so the page fills the window rather than Playwright's
  own fixed viewport.
With no portrait monitor we just `--start-maximized` on the default monitor (the OS
maximize is DPI-correct, so no force-scale is needed there).

Always set:
- `--test-type` hides real Chrome's "unsupported command-line flag" infobar (raised by
  Playwright's default `--disable-blink-features=AutomationControlled`) WITHOUT dropping
  that flag — so navigator.webdriver stays false. The same flag both triggers the bar and
  hides the automation tell, so we keep it; --test-type is Chrome-internal (not visible to
  page JS), so hiding the bar this way adds no detectable signal.

Call ensure_playwright_window_config() before the MCP can launch (app bootstrap, and
any standalone test harness that loads MCP servers itself).
"""
from __future__ import annotations

from pathlib import Path

from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.monitor_utils import vertical_monitor
from app.assistant.utils.path_utils import get_repo_root

CONFIG_REL_PATH = "data/playwright_window_config.json"


def ensure_playwright_window_config() -> Path:
    """Write the gitignored Playwright window config from live monitor detection.
    Idempotent and cheap — safe to call on every boot. Returns the config path."""
    mon = vertical_monitor()  # work-area rect of the portrait monitor, or None
    if mon:
        x, y, w, h = mon
        # --force-device-scale-factor=1: see module docstring — makes --window-size match
        # the physical pixels we detect, so the window fills the monitor instead of
        # overflowing on a scaled profile.
        args = [f"--window-position={x},{y}", f"--window-size={w},{h}",
                "--force-device-scale-factor=1", "--test-type"]
    else:
        # No portrait monitor: OS maximize on the default monitor (DPI-correct on its own).
        args = ["--start-maximized", "--test-type"]
    config = {
        "browser": {
            "launchOptions": {"args": args},
            # Use the full window (no fixed viewport) so the page fills the screen.
            "contextOptions": {"viewport": None},
        }
    }
    path = get_repo_root() / CONFIG_REL_PATH
    write_json_atomic(path, config)
    return path
