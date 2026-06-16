"""Export v2 active beliefs in v1's exact resource shape — a COMPARISON artifact.

Output: resources/kg_derived/resource_user_beliefs_v2.json, written ALONGSIDE v1's
resource_user_beliefs.json (it never clobbers it). Same schema, so the dayflow stages /
insights route / feedback extractor could read it unchanged once we flip the producer; for
now it exists so we can diff the two engines belief-for-belief and prove v2 >= v1 before cutover.

v2's projection is richer than v1's flat row, so a few fields are PROJECTED down to v1's
lossy shape (each documented inline):
  - confidence: v1 wants a "high"/"medium"/"low" BAND; v2 stores `net` (decayed support −
    contradiction). The band is derived from net + contested-status. Consumers use the band
    only as a SORT key (default "low"), never as a filter, so it must order by trustworthiness,
    not calibrate.
  - belief_key: v1's human handle; v2 identity is the canonical `belief_id` — emitted directly.
  - scope: v2 doesn't model scope; mapped from `kind` (durable→chronic, episodic→transient).
    No consumer filters on scope, so this is descriptive only.
  - last_confirmed: v2 `last_observed` (date).

Run: .venv/Scripts/python.exe -m belief_engine_v2.export
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.assistant.utils.path_utils import get_repo_root

_LIVE_DB = get_repo_root() / "belief_engine_v2" / "data" / "belief_v2_live.db"
_OUTPUT_DIR = get_repo_root() / "resources" / "kg_derived"
_OUTPUT_FILE = "resource_user_beliefs_v2.json"

# v2 kind -> v1-style coarse scope (descriptive only; no consumer filters on scope).
_SCOPE_BY_KIND = {
    "semantic_fact": "chronic",
    "preference": "chronic",
    "procedural_routine": "routine",
    "episodic": "transient",
    "transient_state": "transient",
}

# v2 kind -> v1 kind vocabulary (inverse of seed_from_old._KIND_MAP). Consumers FILTER on v1 kind
# names — e.g. the dayflow routine stage keeps only _ROUTINE_RELEVANT_KINDS = {"routine_pattern",
# "durable_fact"} — so a drop-in file must speak v1's vocabulary or every v2 belief is filtered out.
# semantic_fact came from durable_fact|stable_relationship in the seed; durable_fact is the
# routine-relevant target, so we round-trip to it.
_KIND_TO_V1 = {
    "semantic_fact": "durable_fact",
    "preference": "stable_preference",
    "procedural_routine": "routine_pattern",
    "episodic": "episodic_context",
    "transient_state": "transient_state",
}


def _confidence_band(net: float, status: str) -> str:
    """Project v2's decayed `net` (+ contested status) onto v1's sort-band. Consumers use the
    band only as a sort key (default "low"), so this must ORDER by trustworthiness, not
    calibrate. Contested -> low (a disputed belief isn't "settled"); otherwise more confirmed
    net -> higher band. net is already decayed (the projection folds survival into it), so a
    stale single observation lands "low" on its own."""
    if status == "contested":
        return "low"
    if net >= 3.0:
        return "high"
    if net >= 1.0:
        return "medium"
    return "low"


def export_beliefs_v2(*, db_path: Optional[Path] = None, out_name: str = _OUTPUT_FILE) -> Path:
    """Write the v2 active belief set in v1's shape. `out_name` selects the target file:
    the default comparison artifact (resource_user_beliefs_v2.json) or, once v2 is primary,
    the canonical resource_user_beliefs.json. Returns the path written. Raises if the live
    store is missing (fail loud — this is the production store, never a silent empty export)."""
    db = Path(db_path) if db_path is not None else _LIVE_DB
    if not db.exists():
        raise FileNotFoundError(f"belief v2 live store missing: {db}")
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    resource_id = out_name[:-5] if out_name.endswith(".json") else out_name

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # belief_category -> domain; belief_user_override -> statement edit + suppression. Both are
    # optional side tables (survive projection rebuilds); LEFT JOIN so an older store still exports.
    def _has(name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    has_cat, has_ovr, has_sid = _has("belief_category"), _has("belief_user_override"), _has("belief_short_id")
    cols = ["b.*", "c.domain AS _domain" if has_cat else "NULL AS _domain"]
    joins = " LEFT JOIN belief_category c ON c.belief_id=b.belief_id" if has_cat else ""
    if has_ovr:
        cols += ["o.statement_override AS _stmt_ovr"]
        joins += " LEFT JOIN belief_user_override o ON o.belief_id=b.belief_id"
        where = "b.status='active' AND COALESCE(o.suppressed,0)=0"
    else:
        cols += ["NULL AS _stmt_ovr"]
        where = "b.status='active'"
    if has_sid:
        cols += ["s.short_id AS _short_id_n"]
        joins += " LEFT JOIN belief_short_id s ON s.belief_id=b.belief_id"
    else:
        cols += ["NULL AS _short_id_n"]
    rows = conn.execute(
        f"SELECT {', '.join(cols)} FROM beliefs b{joins} WHERE {where} ORDER BY _domain, b.net DESC"
    ).fetchall()
    conn.close()

    entries = []
    for r in rows:
        # owner statement edit wins over the projected text (mirrors the read path)
        statement = r["_stmt_ovr"] if r["_stmt_ovr"] else r["statement_nl"]
        statement = statement.strip() if statement else ""
        if not statement:
            continue
        kind = (r["kind"] or "").strip()
        last_obs = r["last_observed"]
        entry = {
            "belief_key": r["belief_id"],
            "short_id": f"b{r['_short_id_n']}" if r["_short_id_n"] is not None else None,
            "domain": r["_domain"] if r["_domain"] else "general",
            "statement": statement,
            "confidence": _confidence_band(r["net"], r["status"]),
            "scope": _SCOPE_BY_KIND.get(kind.lower(), "chronic"),
            "status": "active",
            "observation_count": r["obs_count"],
            "first_observed": r["first_observed"],
            "last_confirmed": last_obs[:10] if last_obs else "",
        }
        if kind:
            # emit v1-vocab kind so consumers' kind filters (drop-in) match
            entry["kind"] = _KIND_TO_V1.get(kind.lower(), kind)
        entries.append(entry)

    resource = {
        "_metadata": {
            "resource_id": resource_id,
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "engine": "belief_engine_v2",
            "source_db": db.name,
            "domain_filter": "all",
            "entry_count": len(entries),
            "description": (
                "COMPARISON export — belief-engine v2 active beliefs rendered in v1's resource "
                "shape, written alongside resource_user_beliefs.json for side-by-side diff before "
                "cutover. Do not edit manually."
            ),
        },
        "beliefs": entries,
    }

    out = _OUTPUT_DIR / out_name
    # Atomic write: temp file in the same dir, then rename (replace is atomic on one filesystem).
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(resource, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)
    return out


if __name__ == "__main__":
    path = export_beliefs_v2()
    meta = json.loads(path.read_text(encoding="utf-8"))["_metadata"]
    print(f"Exported {meta['entry_count']} v2 beliefs -> {path}")
