"""Persist the DJ's enabled/continuous flags across restarts.

These lived only in memory (DJManager.__init__ set them False), so every
server restart booted the DJ OFF and waited for someone to re-click the /music
toggle. The user restarts often; music quietly stopped being part of the day
for ~3 months. The flags now survive a restart the way every other runtime
toggle in the system does: written on change, restored on boot, in a small
JSON under the writable data dir.
"""
from __future__ import annotations

import json

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_data_dir

logger = get_logger(__name__)

_REL = "dj_state.json"


def _path():
    return get_data_dir() / _REL


def load_dj_state() -> dict:
    """Return {'enabled': bool, 'continuous_mode': bool}. Missing/unreadable
    file means never-configured -> DJ off (the safe default, stated)."""
    p = _path()
    if not p.exists():
        return {"enabled": False, "continuous_mode": False}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return {
            "enabled": bool(d.get("enabled", False)),
            "continuous_mode": bool(d.get("continuous_mode", False)),
        }
    except Exception as e:
        logger.warning("[dj_state] could not read %s (%s) — defaulting DJ off", p, e)
        return {"enabled": False, "continuous_mode": False}


def save_dj_state(*, enabled: bool, continuous_mode: bool) -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(enabled),
                                 "continuous_mode": bool(continuous_mode)}),
                     encoding="utf-8")
    except Exception as e:
        logger.error("[dj_state] could not persist DJ state to %s: %s", p, e)
