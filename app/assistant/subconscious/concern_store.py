"""The concerns the subconscious is tracking — a table in emi.db (2026-09-29).

It replaces resources/subconscious/resource_concerns_register.json. The register's shape is kept as
the domain model — {active, addressing, resolved, dormant} lists of concern records, in order —
because every writer's lifecycle logic (persist.py) and every reader works on it; only storage
moves. One row per concern:

    concern_id            primary key; brain_events.concern_ids point here
    status                active | addressing | resolved | dormant (the register bucket)
    position              order within its bucket
    title, subject, kind, severity, horizon, anchor, first_observed, last_reinforced_utc,
    resolved_at_utc, dormant_at_utc, user_declined_at_utc
                          the tracked state, as columns so the concerns can be queried and joined
    data                  the full record (evidence, journal, work outcomes, escalation…) as JSON

`save_register` writes the whole register in one transaction, so a reader never sees half a tick.
Writers serialize on persist._REGISTER_LOCK (in-process), as they did on the file.

The JSON file is moved in once by `import_legacy_file` (run by hand) and renamed to
`*.imported-<date>.json` so it cannot be read by mistake; nothing reads it after that.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.assistant.subconscious.db import connect as _connect
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

BUCKETS = ("active", "addressing", "resolved", "dormant")
_COLUMNS = ("title", "subject", "kind", "severity", "horizon", "anchor", "first_observed",
            "last_reinforced_utc", "resolved_at_utc", "dormant_at_utc", "user_declined_at_utc")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS concerns (
        concern_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        position INTEGER NOT NULL,
        title TEXT NOT NULL,
        subject TEXT, kind TEXT, severity TEXT, horizon TEXT, anchor TEXT, first_observed TEXT,
        last_reinforced_utc TEXT, resolved_at_utc TEXT, dormant_at_utc TEXT, user_declined_at_utc TEXT,
        updated_at TEXT NOT NULL,
        data TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS concerns_status ON concerns(status, position)",
    "CREATE INDEX IF NOT EXISTS concerns_anchor ON concerns(anchor)",
    "CREATE TABLE IF NOT EXISTS concern_register_meta (key TEXT PRIMARY KEY, value TEXT)",
]
_META_KEYS = ("schema_version", "last_updated_utc", "last_noticer_tick_utc")


def _legacy_file() -> Path:
    from app.assistant.utils.path_utils import get_resources_dir
    return get_resources_dir() / "subconscious" / "resource_concerns_register.json"


def ensure_schema(connect=None) -> None:
    connect = connect or _connect
    with connect(True) as c:
        for statement in SCHEMA:
            c.execute(statement)


def _empty() -> Dict[str, Any]:
    return {"schema_version": 1, "last_updated_utc": None, "last_noticer_tick_utc": None,
            **{b: [] for b in BUCKETS}}


def _read(connect) -> Dict[str, Any]:
    register = _empty()
    with connect(False) as c:
        for key, value in c.execute("SELECT key, value FROM concern_register_meta"):
            register[key] = json.loads(value)
        for status, data in c.execute("SELECT status, data FROM concerns ORDER BY status, position"):
            if status not in BUCKETS:
                raise ValueError(f"concern row with unknown status {status!r}")
            register[status].append(json.loads(data))
    return register


def load_register(*, connect=None) -> Dict[str, Any]:
    """The whole register, in bucket order. Reads the table only; the legacy JSON file is imported
    by `import_legacy_file`, run once by hand, never as a side effect of a read."""
    connect = connect or _connect
    ensure_schema(connect)
    return _read(connect)


def save_register(register: Dict[str, Any], *, connect=None) -> None:
    """Write the whole register in one transaction. A concern absent from every bucket is removed —
    the register is the complete state, as the file was."""
    connect = connect or _connect
    ensure_schema(connect)
    now = datetime.now(timezone.utc).isoformat()
    rows, seen = [], set()
    for status in BUCKETS:
        for position, c in enumerate(register.get(status) or []):
            cid = str(c.get("concern_id") or "").strip()
            if not cid:
                raise ValueError(f"a {status} concern has no concern_id: {str(c.get('title'))[:80]!r}")
            if cid in seen:
                raise ValueError(f"concern {cid} appears in more than one bucket")
            seen.add(cid)
            rows.append((cid, status, position, str(c.get("title") or ""),
                         *[None if c.get(k) is None else str(c.get(k)) for k in _COLUMNS[1:]],
                         now, json.dumps(c, ensure_ascii=False)))
    with connect(True) as c:
        existing = {r[0] for r in c.execute("SELECT concern_id FROM concerns")}
        gone = existing - seen
        if gone:
            c.executemany("DELETE FROM concerns WHERE concern_id=?", [(g,) for g in gone])
        c.executemany(
            "INSERT INTO concerns (concern_id, status, position, title, subject, kind, severity, horizon, anchor, "
            "first_observed, last_reinforced_utc, resolved_at_utc, dormant_at_utc, user_declined_at_utc, "
            "updated_at, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(concern_id) DO UPDATE SET status=excluded.status, position=excluded.position, "
            "title=excluded.title, subject=excluded.subject, kind=excluded.kind, severity=excluded.severity, "
            "horizon=excluded.horizon, anchor=excluded.anchor, first_observed=excluded.first_observed, "
            "last_reinforced_utc=excluded.last_reinforced_utc, resolved_at_utc=excluded.resolved_at_utc, "
            "dormant_at_utc=excluded.dormant_at_utc, user_declined_at_utc=excluded.user_declined_at_utc, "
            "updated_at=excluded.updated_at, data=excluded.data", rows)
        c.executemany("INSERT OR REPLACE INTO concern_register_meta (key, value) VALUES (?, ?)",
                      [(k, json.dumps(register.get(k))) for k in _META_KEYS])
    if gone:
        logger.warning("[concern_store] %d concern(s) removed from the register: %s", len(gone), sorted(gone))


def import_legacy_file(path: Path | None = None, *, connect=None) -> int:
    """One-time move of resource_concerns_register.json into the concerns table.

    Refuses when the table already holds concerns (it would overwrite them) and when the file is
    missing. Renames the file to `*.imported-<date>.json` afterwards so nothing can read it by
    mistake. Explicit on purpose: an implicit import on first read fired from a test on
    2026-09-29 (a test database is empty, the real file is not) and renamed the real file.

        .venv/Scripts/python.exe -m app.assistant.subconscious.concern_store --import-legacy
    """
    path = path or _legacy_file()
    connect = connect or _connect
    ensure_schema(connect)
    if not path.is_file():
        raise FileNotFoundError(f"no legacy register at {path}")
    with connect(False) as c:
        held = c.execute("SELECT COUNT(*) FROM concerns").fetchone()[0]
    if held:
        raise RuntimeError(f"the concerns table already holds {held} concern(s); refusing to import over them")
    register = json.loads(path.read_text(encoding="utf-8"))
    count = sum(len(register.get(b) or []) for b in BUCKETS)
    save_register(register, connect=connect)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    path.rename(path.with_name(f"{path.stem}.imported-{stamp}.json"))
    logger.info("[concern_store] imported %d concern(s) from %s into the concerns table", count, path)
    return count


def import_legacy_file_if_pending() -> int:
    """App startup (app/bootstrap.py): move the legacy file in when the table is still empty.

    Runs only from the app's own startup, never from a read or from the test bootstrap, and uses
    the app's resources dir. A file left beside a populated table means something wrote the old
    file after the move; that is logged as an error and nothing is overwritten."""
    path = _legacy_file()
    if not path.is_file():
        return 0
    ensure_schema()
    with _connect(False) as c:
        held = c.execute("SELECT COUNT(*) FROM concerns").fetchone()[0]
    if held:
        logger.error("[concern_store] %s exists but the concerns table already holds %d concern(s); "
                     "the file is NOT read. Something wrote the old register after the move.", path, held)
        return 0
    return import_legacy_file(path)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    if sys.argv[1:] != ["--import-legacy"]:
        raise SystemExit("usage: python -m app.assistant.subconscious.concern_store --import-legacy")
    import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI and the database
    print(f"imported {import_legacy_file()} concern(s)")
