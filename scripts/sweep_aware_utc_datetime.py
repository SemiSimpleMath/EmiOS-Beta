"""One-shot sweep: swap `DateTime(timezone=True)` → `AwareUtcDateTime`
across all model files, adding the import where missing.

Run from the repo root:
    .venv\\Scripts\\python.exe scripts\\sweep_aware_utc_datetime.py

Idempotent — re-running is safe; files already swapped are skipped.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


FILES = [
    "app/assistant/database/kg_node_verdict.py",
    "app/assistant/kg_core/taxonomy/models.py",
    "app/assistant/database/kg_maintenance_finding.py",
    "app/assistant/ticket_manager/ticket.py",
    "app/assistant/lib/google_auth/oauth_account_store.py",
    "app/assistant/entity_management/entity_cards.py",
    "app/assistant/database/kg_pipeline_models.py",
    "app/assistant/database/kg_revision_log.py",
    "app/assistant/database/kg_merge_log.py",
    "app/assistant/database/claim_proposals.py",
    "app/assistant/database/kg_chat_projection.py",
    "app/models/sleep_segments.py",
    "app/models/wake_segments.py",
    "app/models/maintenance_logs.py",
    "app/models/duplicate_node_tracking.py",
    "app/models/active_segments.py",
    "app/assistant/scheduler/storage/event_storage.py",
    "app/assistant/kg_review/data_models/kg_review.py",
    "app/assistant/kg_repair_pipeline/data_models/node_processing_tracking.py",
    "app/assistant/event_graph/event_node.py",
    "app/assistant/database/processed_entity_log.py",
    "app/assistant/database/entity_card_maintenance_finding.py",
]

IMPORT_LINE = "from app.assistant.utils.time_utils import AwareUtcDateTime"


def _add_import(text: str) -> str:
    """Add the AwareUtcDateTime import after the last existing import line.

    If an import from time_utils already exists, extend it; otherwise
    append a new import after the last `from ... import ...` line at the
    top of the file.
    """
    # Detect the import explicitly — checking for "AwareUtcDateTime" in
    # text was buggy: column-type usages (Column(AwareUtcDateTime, ...))
    # match the substring too and would short-circuit the import add.
    if IMPORT_LINE in text:
        return text
    pattern_existing = re.compile(
        r"^from app\.assistant\.utils\.time_utils import .*AwareUtcDateTime",
        re.MULTILINE,
    )
    if pattern_existing.search(text):
        return text

    # Try to extend an existing time_utils import.
    pattern = re.compile(
        r"^(from app\.assistant\.utils\.time_utils import )([^\n]+)$",
        re.MULTILINE,
    )
    m = pattern.search(text)
    if m:
        existing = m.group(2).rstrip()
        if "AwareUtcDateTime" in existing:
            return text
        new_line = f"{m.group(1)}{existing}, AwareUtcDateTime"
        return text[:m.start()] + new_line + text[m.end():]

    # Otherwise inject a fresh import after the last top-of-file import.
    lines = text.split("\n")
    last_import_idx = -1
    for i, line in enumerate(lines):
        if line.startswith("from ") or line.startswith("import "):
            # Stop at first non-import after we've seen at least one.
            last_import_idx = i
        elif last_import_idx >= 0 and line.strip() and not line.startswith(("#", '"', "'")):
            break
    if last_import_idx < 0:
        # No imports found — drop at top.
        return IMPORT_LINE + "\n" + text
    lines.insert(last_import_idx + 1, IMPORT_LINE)
    return "\n".join(lines)


def _swap_columns(text: str) -> tuple[str, int]:
    """Replace `DateTime(timezone=True)` with `AwareUtcDateTime`. Returns
    (new_text, swap_count)."""
    new_text, count = re.subn(
        r"\bDateTime\(timezone=True\)",
        "AwareUtcDateTime",
        text,
    )
    return new_text, count


def process(path: Path) -> tuple[int, bool]:
    """Process one file. Returns (swap_count, import_added).

    Two passes:
    1. Swap any remaining `DateTime(timezone=True)` → `AwareUtcDateTime`.
    2. If the file references `AwareUtcDateTime` (from this run or a
       prior partial run) and is missing the import, add it.
    """
    text = path.read_text(encoding="utf-8")
    new_text, count = _swap_columns(text)
    needs_import = (
        "AwareUtcDateTime" in new_text and IMPORT_LINE not in new_text
        and not re.search(
            r"^from app\.assistant\.utils\.time_utils import .*AwareUtcDateTime",
            new_text,
            re.MULTILINE,
        )
    )
    if needs_import:
        new_text = _add_import(new_text)
    if new_text == text:
        return 0, False
    path.write_text(new_text, encoding="utf-8")
    return count, needs_import


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    total_swaps = 0
    files_changed = 0
    for rel in FILES:
        path = repo_root / rel
        if not path.exists():
            print(f"  ?? {rel}: not found")
            continue
        count, import_added = process(path)
        if count == 0 and not import_added:
            print(f"  -- {rel}: nothing to do")
            continue
        files_changed += 1
        total_swaps += count
        if count and import_added:
            marker = f"{count} swap(s) +import"
        elif count:
            marker = f"{count} swap(s)"
        else:
            marker = "+import only"
        print(f"  ok {rel}: {marker}")
    print()
    print(f"Done: {total_swaps} swaps across {files_changed} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
