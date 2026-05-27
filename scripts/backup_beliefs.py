"""Belief-store backup.

Dumps the canonical belief data to a timestamped folder under
data/backups/beliefs/. Captures:

  1. user_beliefs        — all rows as JSON (every field)
  2. belief_evidence     — all rows as JSON (provenance trail)
  3. resource_user_beliefs.json — the exported projection downstream
                                  agents read

Chroma embeddings are NOT captured. They're regenerable from belief
text by re-running the embed step; storing them here would double the
backup size for redundant data.

Restore is manual: replace the rows back into emi.db with the same
schema (no ID renumbering needed since we use string IDs).

Run via: .venv\\Scripts\\python.exe scripts\\backup_beliefs.py
"""
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import sys


def main():
    repo_root = Path(__file__).resolve().parent.parent
    db_path = repo_root / "emi.db"
    if not db_path.exists():
        print(f"ERROR: emi.db not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = repo_root / "data" / "backups" / "beliefs" / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Backup destination: {out_dir}")

    # 1. Dump SQLite tables as JSON.
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        for table in ("user_beliefs", "belief_evidence"):
            cur = conn.execute(f"SELECT * FROM {table}")
            rows = [dict(r) for r in cur.fetchall()]
            (out_dir / f"{table}.json").write_text(
                json.dumps(rows, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"  {table}.json — {len(rows)} rows")
    finally:
        conn.close()

    # 2. Copy the canonical exported projection.
    src = repo_root / "resources" / "kg_derived" / "resource_user_beliefs.json"
    if src.exists():
        dst = out_dir / "resource_user_beliefs.json"
        shutil.copy2(src, dst)
        size = dst.stat().st_size
        print(f"  resource_user_beliefs.json — {size:,} bytes (copied from {src.relative_to(repo_root)})")
    else:
        print(f"  WARN: {src.relative_to(repo_root)} does not exist; skipped.", file=sys.stderr)

    # 3. Write a manifest with version info / git ref for traceability.
    manifest = {
        "backup_ts_utc": ts,
        "db_path": str(db_path.relative_to(repo_root)),
        "tables": ["user_beliefs", "belief_evidence"],
        "resource_files": [
            str((src.relative_to(repo_root))) if src.exists() else None,
        ],
        "note": "Created before belief_canonicalizer engine swap + canonicalization changes",
    }
    try:
        import subprocess
        rev = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, stderr=subprocess.DEVNULL,
        ).decode().strip()
        manifest["git_head"] = rev
    except Exception:
        manifest["git_head"] = None
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    print()
    print(f"OK - Backup complete: {out_dir.relative_to(repo_root)}")


if __name__ == "__main__":
    main()
