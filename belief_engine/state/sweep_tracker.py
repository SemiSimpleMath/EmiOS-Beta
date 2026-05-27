"""Tracks when belief canonicalization last did a full cross-belief sweep.

The canonicalizer has two modes (decided per run by `decide_mode`):

  - "new_only" (default 6 of 7 nights) — only canonicalize beliefs created
    since the last full sweep. Cheap: usually ~5 new beliefs total across
    all domains.

  - "full" (1 of 7 nights, or first run) — full cross-belief sweep of
    every active belief in each domain. Catches drift / reevaluation
    rewrites / cross-belief duplicates that the new-only mode skipped.

State is a single timestamp in a JSON file. Atomic via temp-file rename
so concurrent reads don't see a half-written file. File is gitignored
(`data/` is in .gitignore).

Bootstrap behavior: first run sees `last_full_sweep_at` missing →
forces "full" mode that night. After that, the cadence stabilizes at
weekly.
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


def read_last_full_sweep_at() -> Optional[datetime]:
    """Returns the UTC timestamp of the last completed full sweep, or None
    if no sweep has been recorded yet (first-run / file-missing case).
    """
    p = _state_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        raw = data.get("last_full_sweep_at")
        if not isinstance(raw, str) or not raw.strip():
            return None
        # Parse ISO 8601. Handle trailing Z and offset shapes.
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception as e:
        logger.warning("[sweep_tracker] failed to read %s: %s", p, e)
        return None


def mark_full_sweep_completed() -> None:
    """Record that a full sweep just completed (now, UTC).

    Atomic write — temp file + rename — so a crash mid-write doesn't
    corrupt the state file.
    """
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    payload = {"last_full_sweep_at": now_iso}

    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=".belief_state_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, p)
        logger.info("[sweep_tracker] marked full sweep completed at %s", now_iso)
    except Exception:
        # Clean up the temp file if rename failed
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise


def decide_mode(*, now_utc: Optional[datetime] = None, interval_days: int = FULL_SWEEP_INTERVAL_DAYS) -> Mode:
    """Decide whether tonight is a full-sweep night or new-only night.

    Returns "full" when:
      - No prior full sweep has been recorded (bootstrap case), OR
      - It's been at least `interval_days` since the last full sweep.

    Otherwise returns "new_only".
    """
    last = read_last_full_sweep_at()
    if last is None:
        logger.info("[sweep_tracker] no prior full sweep recorded → mode=full (bootstrap)")
        return "full"
    now = now_utc or datetime.now(timezone.utc)
    elapsed_days = (now - last).total_seconds() / 86400.0
    if elapsed_days >= interval_days:
        logger.info(
            "[sweep_tracker] %.1f days since last full sweep (>= %d) → mode=full",
            elapsed_days, interval_days,
        )
        return "full"
    logger.info(
        "[sweep_tracker] %.1f days since last full sweep (< %d) → mode=new_only",
        elapsed_days, interval_days,
    )
    return "new_only"
