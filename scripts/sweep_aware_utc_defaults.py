"""One-shot sweep: replace `server_default=func.now()` with `default=utc_now`
on AwareUtcDateTime columns. Same target as the AwareUtcDateTime sweep —
SQLite's func.now() returns a naive ISO string and bypasses the bind
path. utc_now (a Python callable returning aware UTC) does not.

Also handles `onupdate=func.now()` → `onupdate=utc_now`.

Run from the repo root:
    .venv\\Scripts\\python.exe scripts\\sweep_aware_utc_defaults.py

Idempotent — re-running is safe; files already swapped are skipped.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


FILES = [
    "app/assistant/event_graph/event_node.py",
    "app/assistant/entity_management/entity_cards.py",
    "app/assistant/database/processed_entity_log.py",
    "app/models/sleep_segments.py",
    "app/assistant/database/kg_revision_log.py",
    "app/models/node_analysis_tracking.py",
    "app/assistant/database/kg_pipeline_models.py",
    "app/assistant/database/kg_merge_log.py",
    "app/models/maintenance_logs.py",
    "app/assistant/database/kg_maintenance_finding.py",
    "app/assistant/database/claim_proposals.py",
    "app/assistant/database/kg_chat_projection.py",
    "app/models/active_segments.py",
    "app/assistant/database/entity_card_maintenance_finding.py",
    "app/assistant/ticket_manager/ticket.py",
]


def _has_aware_decorator(text: str) -> bool:
    return "AwareUtcDateTime" in text


def _ensure_utc_now_import(text: str) -> str:
    """Make sure `utc_now` is imported alongside AwareUtcDateTime."""
    # Already importing utc_now?
    if re.search(
        r"^from app\.assistant\.utils\.time_utils import [^\n]*\butc_now\b",
        text, re.MULTILINE,
    ):
        return text

    # Extend the existing AwareUtcDateTime import if there is one.
    pattern = re.compile(
        r"^(from app\.assistant\.utils\.time_utils import )([^\n]+)$",
        re.MULTILINE,
    )
    m = pattern.search(text)
    if m:
        existing = m.group(2).rstrip()
        if "utc_now" in existing:
            return text
        new_line = f"{m.group(1)}{existing}, utc_now"
        return text[:m.start()] + new_line + text[m.end():]

    # Otherwise inject after the AwareUtcDateTime import line.
    pattern2 = re.compile(
        r"^(from [^\n]*\bAwareUtcDateTime\b[^\n]*)$",
        re.MULTILINE,
    )
    m2 = pattern2.search(text)
    if m2:
        line = m2.group(1)
        return (
            text[:m2.end()]
            + f"\nfrom app.assistant.utils.time_utils import utc_now"
            + text[m2.end():]
        )

    # Last resort: prepend a new import.
    return "from app.assistant.utils.time_utils import utc_now\n" + text


def _swap_defaults(text: str) -> tuple[str, int]:
    """Replace `server_default=func.now()` → `default=utc_now` and
    `onupdate=func.now()` → `onupdate=utc_now`. Returns (new_text, count)."""
    new_text = text
    count = 0
    n1 = new_text.count("server_default=func.now()")
    new_text = new_text.replace("server_default=func.now()", "default=utc_now")
    n2 = new_text.count("onupdate=func.now()")
    new_text = new_text.replace("onupdate=func.now()", "onupdate=utc_now")
    count = n1 + n2
    return new_text, count


def _maybe_drop_unused_func_import(text: str) -> str:
    """If `func` is no longer referenced anywhere outside the import line,
    drop it from the sqlalchemy import to keep the imports lean."""
    # Count references (excluding the import line itself).
    lines = text.split("\n")
    other_refs = 0
    for line in lines:
        if line.startswith("from sqlalchemy") and "import" in line and "func" in line:
            continue
        if re.search(r"\bfunc\.", line):
            other_refs += 1
    if other_refs > 0:
        return text

    # Drop func from `from sqlalchemy import (...)` blocks.
    # Single-line: `from sqlalchemy import A, func, B` → `from sqlalchemy import A, B`
    text = re.sub(
        r"(from sqlalchemy import [^\n]*),\s*func\b",
        r"\1",
        text,
    )
    text = re.sub(
        r"(from sqlalchemy import )func,\s*",
        r"\1",
        text,
    )
    # Multi-line: `    func, ...` inside paren block
    text = re.sub(
        r"^(    [^)]*),\s*func\b",
        r"\1",
        text, flags=re.MULTILINE,
    )
    text = re.sub(
        r"^(    )func,\s*",
        r"\1",
        text, flags=re.MULTILINE,
    )
    # Stand-alone import on its own line
    text = re.sub(
        r"^from sqlalchemy\.sql import func\n",
        "",
        text, flags=re.MULTILINE,
    )
    return text


def process(path: Path) -> tuple[int, bool]:
    text = path.read_text(encoding="utf-8")
    if not _has_aware_decorator(text):
        return 0, False
    new_text, count = _swap_defaults(text)
    if count == 0:
        return 0, False
    new_text = _ensure_utc_now_import(new_text)
    # Don't aggressively drop func — too risky for one-off cleanup. Leave it.
    if new_text == text:
        return 0, False
    path.write_text(new_text, encoding="utf-8")
    return count, True


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    total = 0
    files_changed = 0
    for rel in FILES:
        path = repo_root / rel
        if not path.exists():
            print(f"  ?? {rel}: not found")
            continue
        count, changed = process(path)
        if not changed:
            print(f"  -- {rel}: nothing to do")
            continue
        total += count
        files_changed += 1
        print(f"  ok {rel}: {count} default(s) swapped")
    print()
    print(f"Done: {total} swaps across {files_changed} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
