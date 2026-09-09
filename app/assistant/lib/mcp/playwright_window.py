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

import json
from pathlib import Path

from app.assistant.utils.atomic_write import write_json_atomic
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.monitor_utils import vertical_monitor
from app.assistant.utils.path_utils import get_data_dir, get_resources_dir

logger = get_logger(__name__)

CONFIG_REL_PATH = "data/playwright_window_config.json"

# Browser-chrome noise that blocks automation and is invisible to page-level
# perception (a11y snapshot / DOM scan / page screenshot all miss it):
# - the "Restore pages? Chrome didn't shut down correctly" chip, which appears
#   because the MCP session is killed uncleanly between runs and then overlays
#   the top of the page.
_NOISE_SUPPRESSION_ARGS = ["--hide-crash-restore-bubble", "--disable-session-crashed-bubble"]


def _resolve_home_geolocation() -> dict | None:
    """The assistant's known home coordinate for the automation browser, or None.

    Reads resource_current_location.json and returns {"latitude","longitude"}
    ONLY when precise coordinates are present. Returns None otherwise — the
    caller then leaves the browser to prompt rather than granting geolocation
    with no position (which makes sites see a position-unavailable error, worse
    than the prompt). Best-effort: never raises into boot.
    """
    try:
        p = get_resources_dir() / "resource_current_location.json"
        if not p.exists():
            return None
        loc = (json.loads(p.read_text(encoding="utf-8")) or {}).get("current_location")
        if not isinstance(loc, dict):
            return None
        lat, lon = loc.get("latitude"), loc.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return {"latitude": float(lat), "longitude": float(lon)}
        # No precise coords in the live resource yet — the location plumbing is
        # city-only for now. Fall back to a machine-local manual anchor set in
        # .env (EMI_BROWSER_GEOLOCATION="lat,lon"). Prefers the live resource
        # above; this is the standing "home" until the plumbing emits coords.
        env = _geolocation_from_env()
        if env:
            return env
        logger.info(
            "[playwright_window] no precise coords (resource city=%r, no "
            "EMI_BROWSER_GEOLOCATION); geolocation not pre-granted — sites prompt.",
            (loc.get("address") or {}).get("city") if isinstance(loc.get("address"), dict) else None,
        )
    except Exception as e:
        logger.warning("[playwright_window] home geolocation read failed: %s", e)
    return _geolocation_from_env()


def _geolocation_from_env() -> dict | None:
    """Parse EMI_BROWSER_GEOLOCATION='lat,lon' (machine-local, gitignored .env)."""
    import os
    raw = (os.environ.get("EMI_BROWSER_GEOLOCATION") or "").strip()
    if not raw:
        return None
    try:
        lat_s, lon_s = raw.split(",", 1)
        return {"latitude": float(lat_s.strip()), "longitude": float(lon_s.strip())}
    except (ValueError, TypeError):
        logger.warning("[playwright_window] EMI_BROWSER_GEOLOCATION not 'lat,lon': %r", raw)
        return None


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
    # Suppress the "Restore pages?" crash-restore chip (browser chrome the agent
    # can't see or dismiss; appears because the MCP session is killed uncleanly).
    args = args + _NOISE_SUPPRESSION_ARGS

    context_options: dict = {"viewport": None}  # full window, no fixed viewport
    geo = _resolve_home_geolocation()
    if geo:
        # Pre-grant geolocation with the known home coordinate so location-gated
        # flows ("near me", delivery radius) work without the native permission
        # bubble — which is browser chrome the agent can't click. Grant ONLY
        # with a coordinate; granting without one makes sites see a
        # position-unavailable error, which is worse than the prompt.
        context_options["permissions"] = ["geolocation"]
        context_options["geolocation"] = geo

    config = {
        "browser": {
            "launchOptions": {"args": args},
            "contextOptions": context_options,
        }
    }
    # get_data_dir(), NOT get_repo_root(): this file is WRITTEN on every boot, and the
    # code tree is read-only in a packaged install (the Linux AppImage is a read-only
    # squashfs), where the write raised OSError(30) and took down startup. In dev
    # EMI_DATA_DIR is unset, so get_data_dir() IS the repo root and the path is
    # byte-for-byte unchanged.
    path = get_data_dir() / CONFIG_REL_PATH
    write_json_atomic(path, config)
    return path
