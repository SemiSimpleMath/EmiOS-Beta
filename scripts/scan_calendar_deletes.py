"""
Scan Emi log files from a start date onward for any calendar-mutating tool
INVOCATIONS (not prompt-doc mentions).

Targets:
  - delete_calendar_event
  - delete_recurring_calendar_event
  - update_calendar_event

Invocation patterns (from tool_caller and agent planner outputs):
  - tool_caller: "Executing target: '<tool>'"
  - tool_caller: "Calling tool: <tool>" / "Calling standard tool: <tool>"
  - planner MODEL_OUTPUT: "\"action\": \"<tool>\"" or "action: <tool>"
  - shared::tool_arguments dispatch: "target_name = <tool>"

Usage:
    .venv/Scripts/python.exe scripts/scan_calendar_deletes.py --since 2026-04-10
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path
from datetime import datetime

# Force utf-8 stdout so emoji/unicode bytes in log lines don't crash the print loop.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
TOOLS = [
    "delete_calendar_event",
    "delete_recurring_calendar_event",
    "update_calendar_event",
]

# Build invocation patterns. These only match when the tool name appears in an
# invocation-ish context — not when it's listed in a prompt's tool catalog.
INVOCATION_PATTERNS = []
for t in TOOLS:
    te = re.escape(t)
    INVOCATION_PATTERNS.extend([
        rf"Executing target:\s*['\"]?{te}['\"]?",
        rf"Calling tool:\s*{te}",
        rf"Calling standard tool:\s*{te}",
        rf'"tool_name"\s*:\s*"{te}"',
        rf'"action"\s*:\s*"{te}"',
        rf"\btarget_name\b\s*=\s*{te}",
        rf"tool_name='{te}'",
    ])
PATTERN = re.compile("|".join(INVOCATION_PATTERNS))

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)")


def file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except Exception:
        return None


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if PATTERN.search(line):
                    m = TS_RE.match(line)
                    ts = m.group(0) if m else ""
                    hits.append((i, ts, line.rstrip("\n")))
    except Exception as e:
        print(f"[WARN] Could not read {path.name}: {e}")
    return hits


def context_window(path: Path, hit_line: int, before: int, after: int) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception:
        return []
    lo = max(0, hit_line - 1 - before)
    hi = min(len(lines), hit_line + after)
    return [lines[j].rstrip("\n") for j in range(lo, hi)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--context", type=int, default=6, help="lines of context per hit")
    ap.add_argument("--include-agents", action="store_true",
                    help="also scan *_agents.log (MODEL_OUTPUT includes planner actions)")
    ap.add_argument("--truncate", type=int, default=280, help="max chars per printed line")
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d")

    all_files = sorted(LOG_DIR.glob("emi_logs_*.log"))
    files = []
    for f in all_files:
        if not args.include_agents and f.name.endswith("_agents.log"):
            continue
        mt = file_mtime(f)
        if mt is None or mt < since:
            continue
        files.append(f)

    def squish(s: str) -> str:
        if len(s) > args.truncate:
            return s[: args.truncate] + " …"
        return s

    total_hits = 0
    for f in files:
        hits = scan_file(f)
        if not hits:
            continue
        mt = file_mtime(f)
        print(f"\n=== {f.name}  (mtime={mt.strftime('%Y-%m-%d %H:%M') if mt else '?'})  {len(hits)} hit(s) ===")
        for line_no, log_ts, matched in hits:
            print(f"\n[line {line_no}  {log_ts or '(no ts)'}]")
            print(f"  >> {squish(matched)}")
            if args.context > 0:
                for c in context_window(f, line_no, before=args.context, after=args.context):
                    print(f"     | {squish(c)}")
        total_hits += len(hits)

    print(f"\n--- scanned {len(files)} file(s) since {args.since}  |  total invocation hits: {total_hits}")


if __name__ == "__main__":
    main()
