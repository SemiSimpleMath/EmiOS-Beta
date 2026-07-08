"""Tracks when belief canonicalization last did a full cross-belief sweep — PER DOMAIN.

The canonicalizer has two modes (decided per domain by `decide_mode`):

  - "new_only" (default 6 of 7 nights) — only canonicalize beliefs created
    since the domain's last full sweep. Cheap: usually ~5 new beliefs total
    across all domains.

  - "full" (1 of 7 nights, or first run) — full cross-belief sweep of
    every active belief in the domain. Catches drift / reevaluation
    rewrites / cross-belief duplicates that the new-only mode skipped.

State is a JSON file: a `domains` map of per-domain timestamps, plus the
legacy global `last_full_sweep_at`, which domains without their own stamp
read. Per-domain stamping (each domain stamps as ITS full sweep completes,
inside CanonicalizeBeliefSetStep) is what makes an interrupted multi-domain
run resumable: completed domains stay completed. Atomic via temp-file
rename. File is gitignored (`data/`).

Bootstrap behavior: a domain with no stamp anywhere → "full" that night.
After that, the cadence stabilizes at weekly per domain.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


# Days between full sweeps. Configurable here; default weekly.
FULL_SWEEP_INTERVAL_DAYS = 7


Mode = Literal["full", "new_only"]


def _state_path() -> Path:
    return get_repo_root() / "data" / "belief_engine_state.json"


def _parse_iso(raw) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _read_state() -> dict:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("[sweep_tracker] failed to read %s: %s", p, e)
        return {}


def _write_state(payload: dict) -> None:
    """Atomic write — temp file + rename — so a crash mid-write doesn't corrupt the file."""
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=".belief_state_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, p)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def read_last_full_sweep_at(domain: Optional[str] = None) -> Optional[datetime]:
    """The UTC timestamp of the domain's last completed full sweep. Falls back to the
    legacy global stamp for a domain without its own entry; None when nothing is
    recorded anywhere (first-run / file-missing case). domain=None reads the legacy
    global stamp only."""
    data = _read_state()
    if domain:
        per_domain = data.get("domains")
        if isinstance(per_domain, dict):
            stamped = _parse_iso(per_domain.get(domain))
            if stamped is not None:
                return stamped
    return _parse_iso(data.get("last_full_sweep_at"))


def mark_full_sweep_completed(domain: str) -> None:
    """Record that THIS domain's full sweep just completed (now, UTC). Other domains'
    stamps and the legacy global stamp are preserved."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    data = _read_state()
    per_domain = data.get("domains")
    if not isinstance(per_domain, dict):
        per_domain = {}
    per_domain[str(domain)] = now_iso
    data["domains"] = per_domain
    _write_state(data)
    logger.info("[sweep_tracker] marked full sweep completed for domain=%s at %s", domain, now_iso)


def decide_mode(*, domain: str, now_utc: Optional[datetime] = None,
                interval_days: int = FULL_SWEEP_INTERVAL_DAYS) -> Mode:
    """Decide whether tonight is a full-sweep night for THIS domain.

    Returns "full" when the domain has no recorded full sweep (bootstrap), or when
    at least `interval_days` have passed since its last one. Otherwise "new_only".
    """
    last = read_last_full_sweep_at(domain)
    if last is None:
        logger.info("[sweep_tracker] %s: no prior full sweep recorded → mode=full (bootstrap)", domain)
        return "full"
    now = now_utc or datetime.now(timezone.utc)
    elapsed_days = (now - last).total_seconds() / 86400.0
    if elapsed_days >= interval_days:
        logger.info(
            "[sweep_tracker] %s: %.1f days since last full sweep (>= %d) → mode=full",
            domain, elapsed_days, interval_days,
        )
        return "full"
    logger.info(
        "[sweep_tracker] %s: %.1f days since last full sweep (< %d) → mode=new_only",
        domain, elapsed_days, interval_days,
    )
    return "new_only"
