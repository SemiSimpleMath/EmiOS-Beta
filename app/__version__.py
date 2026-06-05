"""Single source of truth for the application version.

The version string lives in the root ``VERSION`` file (one line, e.g. ``0.1.0``).
CI and the installer read ``VERSION`` directly; runtime code reads it through this
module — ``from app import __version__`` or ``app.__version__``. The tray updater
compares this against the latest GitHub release tag, so bump ``VERSION`` on release.

Kept deliberately import-free (only ``pathlib``) so the version resolves in any
minimal context. ``VERSION`` is a code-layer file (bundled, read-only) one directory
above ``app/`` — it is NOT redirected by ``EMI_DATA_DIR`` (that governs writable
per-user state, not shipped assets).
"""
from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    try:
        text = (Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
        return text or "0.0.0+unknown"
    except Exception:
        return "0.0.0+unknown"


__version__ = _read_version()
