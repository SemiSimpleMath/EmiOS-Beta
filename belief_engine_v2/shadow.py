"""Shadow-run comparison: belief v1 lane vs v2 lane, per meal-planner run.

Both belief engines currently run in parallel — v2 feeds the meal agents
(flag meal_beliefs_v2, default ON) while v1 keeps operating underneath.
This module records, for every v2-lane run, what BOTH lanes produced, so
the tuning session (decay τ, resolver thresholds) and the eventual v1
retirement rest on a week of visible diffs instead of anecdotes.

Storage: JSONL at belief_engine_v2/data/shadow_runs.jsonl (gitignored,
next to the live shadow store). One line per meal-agent run.

Consumers: `shadow_report.py` (CLI) and the /beliefs-shadow page.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_DATA_DIR = Path(__file__).resolve().parent / "data"
SHADOW_LOG_PATH = _DATA_DIR / "shadow_runs.jsonl"
LIVE_DB_PATH = _DATA_DIR / "belief_v2_live.db"


def _slim_v2_row(r: Dict[str, Any]) -> Dict[str, Any]:
    scores = r.get("_scores") or {}
    return {
        "belief_id": r.get("belief_id"),
        "statement": (r.get("statement_nl") or "").strip(),
        "kind": r.get("kind"),
        "status": r.get("status"),
        "last_observed": r.get("last_observed"),
        "obs_count": r.get("obs_count"),
        "score": round(float(r.get("score") or 0.0), 4),
        "scores": {k: round(float(v), 4) for k, v in scores.items()},
    }


def _parse_v1_items(v1_text: str) -> List[str]:
    """The v1 lane renders '- <statement>  [meta]' lines; keep the statements."""
    items: List[str] = []
    for line in (v1_text or "").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        stmt = line[2:]
        # Trim the trailing bracketed meta block when present.
        if stmt.endswith("]") and "  [" in stmt:
            stmt = stmt[: stmt.rfind("  [")]
        items.append(stmt.strip())
    return items


def record_shadow_run(
    *,
    agent: str,
    v2_rows: List[Dict[str, Any]],
    v1_text: str,
    now: Optional[datetime] = None,
    path: Optional[Path] = None,
) -> None:
    """Append one comparison record. Raises on I/O problems — the caller
    decides whether shadow-recording failures may break the run (the meal
    dispatcher wraps this in its own guarded block: shadowing must never
    cost the planner its context)."""
    path = path or SHADOW_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "agent": agent,
        "v2": [_slim_v2_row(r) for r in v2_rows],
        "v1_items": _parse_v1_items(v1_text),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_shadow_runs(*, days: float = 7.0, path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = path or SHADOW_LOG_PATH
    if not path.is_file():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(rec.get("recorded_at") or "") >= cutoff:
            out.append(rec)
    return out


_NORM_RE = re.compile(r"[^a-z0-9 ]+")


def _normalize(stmt: str) -> str:
    return _NORM_RE.sub("", (stmt or "").lower()).strip()


def diff_run(run: Dict[str, Any]) -> Dict[str, Any]:
    """Approximate set diff between the lanes.

    The lanes phrase the same belief differently (v1 renders belief_key
    prose, v2 renders statement_nl), so matching is normalized-substring
    based and labeled APPROXIMATE — the side-by-side view is the ground
    truth; this just directs attention.
    """
    v2_stmts = [(e.get("statement") or "") for e in run.get("v2") or []]
    v1_stmts = [s for s in run.get("v1_items") or []]
    v2_norm = [_normalize(s) for s in v2_stmts]
    v1_norm = [_normalize(s) for s in v1_stmts]

    def _matched(norm: str, pool: List[str]) -> bool:
        if not norm:
            return False
        return any(norm == p or (len(norm) > 20 and (norm in p or p in norm)) for p in pool if p)

    only_v2 = [s for s, n in zip(v2_stmts, v2_norm) if not _matched(n, v1_norm)]
    only_v1 = [s for s, n in zip(v1_stmts, v1_norm) if not _matched(n, v2_norm)]
    return {
        "agent": run.get("agent"),
        "recorded_at": run.get("recorded_at"),
        "v2_count": len(v2_stmts),
        "v1_count": len(v1_stmts),
        "only_v2": only_v2,
        "only_v1": only_v1,
        "approximate": True,
    }


def v2_activity(*, days: float = 7.0, db_path: Optional[Path] = None) -> Dict[str, Any]:
    """What the v2 engine DID in the window: observations folded, beliefs
    minted vs reinforced, resolver method mix, contested beliefs, merges."""
    db_path = db_path or LIVE_DB_PATH
    if not Path(db_path).exists():
        raise FileNotFoundError(f"belief v2 live store missing: {db_path}")
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        observations = con.execute(
            "SELECT COUNT(1) FROM observations WHERE recorded_at >= ?", (cutoff,)
        ).fetchone()[0]
        resolver_mix = {
            r["resolution_method"] or "(none)": r["n"]
            for r in con.execute(
                "SELECT resolution_method, COUNT(1) AS n FROM observations "
                "WHERE recorded_at >= ? GROUP BY resolution_method", (cutoff,)
            )
        }
        minted = [
            dict(r) for r in con.execute(
                "SELECT belief_id, statement_nl, kind, status, obs_count FROM beliefs "
                "WHERE first_observed >= ? ORDER BY first_observed DESC LIMIT 40", (cutoff,)
            )
        ]
        reinforced = [
            dict(r) for r in con.execute(
                "SELECT belief_id, statement_nl, kind, status, obs_count FROM beliefs "
                "WHERE last_observed >= ? AND first_observed < ? "
                "ORDER BY last_observed DESC LIMIT 40", (cutoff, cutoff)
            )
        ]
        contested = [
            dict(r) for r in con.execute(
                "SELECT belief_id, statement_nl, support, contradiction, obs_count "
                "FROM beliefs WHERE status = 'contested' ORDER BY obs_count DESC LIMIT 20"
            )
        ]
        merges = [
            dict(r) for r in con.execute(
                "SELECT belief_id, merged_into, method, created_at FROM belief_merges "
                "WHERE created_at >= ? ORDER BY created_at DESC LIMIT 20", (cutoff,)
            )
        ]
        totals = {
            r["status"]: r["n"]
            for r in con.execute("SELECT status, COUNT(1) AS n FROM beliefs GROUP BY status")
        }
    finally:
        con.close()

    return {
        "window_days": days,
        "observations_folded": observations,
        "resolver_method_mix": resolver_mix,
        "beliefs_minted": minted,
        "beliefs_reinforced": reinforced,
        "contested": contested,
        "merges": merges,
        "belief_totals_by_status": totals,
    }
