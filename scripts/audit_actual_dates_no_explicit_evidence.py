"""audit_actual_dates_no_explicit_evidence.py — repair spurious 'actual' confidence marks.

Symptom (2026-05-11): Katy's timeline shows 84 events in 2026 of which 36 carry
``start_date_confidence='actual'``. Most are dateless sentences ("Katy is
sleeping", "Jukka greets Emi") that got stamped with the chat day. The
meta_data_add prompt says message-date defaults should be `inferred`, but the
agent drifts and emits `actual`. The fact_canonicalizer also rewrites
derived_sentence to embed the chat date ("On 2026-04-05, Jukka said hello"),
which makes text-based detection unable to tell user-stated from
chat-stamp dates.

The user rule (feedback memory ``feedback_estimate_dates_stay_marked``):
"Date-fill agents MUST set start_date_confidence='estimated' or 'inferred'
when deriving from prose. Never 'actual' without user confirmation."

Detection rule (chat-date-stamp signature):
  If start_date equals the date of at least one of the node's evidence
  rows' message_timestamp, it's a chat-stamp default — downgrade to
  'inferred'. Past-event recall (e.g., "we got married September 9, 2003"
  mentioned in a 2026 chat) keeps 'actual' because start_date != chat_date.

Modes:
  --audit       Report counts by detection bucket. No DB ops.
  --commit      Apply downgrades. Requires --yes-write + recent emi.db.bak.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parent.parent
DB_PATH = str(REPO / "emi.db")
BACKUP_PATH = str(REPO / "emi.db.bak")
BACKUP_FRESH_SECONDS = 60 * 60

# Month names + 3-letter abbreviations, both cases.
_MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]
_MONTH_TO_NUM = {m: i + 1 for i, m in enumerate(_MONTHS)}
_MONTH_ABBR = [m[:3] for m in _MONTHS]
_MONTH_ABBR_TO_NUM = {m: i + 1 for i, m in enumerate(_MONTH_ABBR)}


def _normalize(s: str) -> str:
    return (s or "").lower()


def date_appears_in_sentence(date_iso: str, sentence: str) -> bool:
    """Return True iff ``date_iso`` (YYYY-MM-DD) is referenced in ``sentence``
    in any common surface form.

    Conservative on the FALSE side: when in doubt, return False so the row
    gets downgraded to 'inferred'. We'd rather mark a true-actual as inferred
    than leave a spurious actual standing.
    """
    if not date_iso or not sentence:
        return False
    s = _normalize(sentence)
    try:
        year_str, mm_str, dd_str = date_iso[:10].split("-")
        year = int(year_str)
        mm = int(mm_str)
        dd = int(dd_str)
    except (ValueError, IndexError):
        return False

    month_long = _MONTHS[mm - 1]
    month_abbr = _MONTH_ABBR[mm - 1]

    # 1) ISO date "YYYY-MM-DD" or "YYYY/MM/DD"
    if re.search(rf"\b{year}[-/.]{mm:02d}[-/.]{dd:02d}\b", s):
        return True
    # 2) "MM/DD/YYYY" or "DD/MM/YYYY" (we don't distinguish — either is "the date is here")
    if re.search(rf"\b{mm:02d}[/.\-]{dd:02d}[/.\-]{year}\b", s):
        return True
    if re.search(rf"\b{dd:02d}[/.\-]{mm:02d}[/.\-]{year}\b", s):
        return True
    if re.search(rf"\b{mm}[/.\-]{dd}[/.\-]{year}\b", s):
        return True
    if re.search(rf"\b{dd}[/.\-]{mm}[/.\-]{year}\b", s):
        return True
    # 3) "Month Day, Year" / "Month Day Year" / "Mon Day Year"
    #    Day may have ordinal suffix (1st, 2nd, 3rd, 4th).
    day_pat = rf"{dd}(?:st|nd|rd|th)?"
    for mname in (month_long, month_abbr):
        if re.search(rf"\b{mname}\.?\s+{day_pat}(?:,?\s+{year})?\b", s):
            return True
        # 4) "Day Month Year" / "Day of Month, Year"
        if re.search(rf"\b{day_pat}\s+(?:of\s+)?{mname}\.?(?:,?\s+{year})?\b", s):
            return True
        # 5) "Month Year" (specific month — narrower than just year, still acceptable)
        if re.search(rf"\b{mname}\.?,?\s+{year}\b", s):
            return True
    # 6) Bare year — only meaningful when start_date is Jan 1 (a year-only date).
    if mm == 1 and dd == 1 and re.search(rf"\b{year}\b", s):
        return True
    return False


def _evidence_chat_dates(con: sqlite3.Connection, node_id: str) -> set[str]:
    """Distinct YYYY-MM-DD strings from this node's evidence rows'
    message_timestamp. These are the dates of the chats that produced
    the node — the candidate chat-stamp defaults."""
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT message_timestamp
        FROM kg_node_evidence
        WHERE node_id = ? AND message_timestamp IS NOT NULL
        """,
        (node_id,),
    ).fetchall()
    out: set[str] = set()
    for (ts,) in rows:
        if not ts:
            continue
        s = str(ts)
        if len(s) >= 10:
            out.add(s[:10])
    return out


def _start_date_iso(start_date) -> str:
    """Normalize a kg_node_metadata.start_date value (datetime str) to YYYY-MM-DD."""
    if not start_date:
        return ""
    s = str(start_date)
    return s[:10] if len(s) >= 10 else ""


def audit(con: sqlite3.Connection) -> dict:
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT id, node_type, label, start_date, original_sentence, start_date_confidence
        FROM kg_node_metadata
        WHERE node_type IN ('State', 'Event', 'Goal')
          AND start_date IS NOT NULL
          AND lower(coalesce(start_date_confidence, '')) = 'actual'
        """
    ).fetchall()
    total = len(rows)
    kept = 0
    downgrade = 0
    no_evidence = 0
    samples_keep = []
    samples_downgrade = []
    for row in rows:
        nid, ntype, label, start_date, sentence, _ = row
        sd_iso = _start_date_iso(start_date)
        chat_dates = _evidence_chat_dates(con, nid)
        if not chat_dates:
            # No evidence rows — can't tell if it's chat-stamped. Be
            # conservative: keep the 'actual' as-is.
            no_evidence += 1
            kept += 1
            if len(samples_keep) < 8:
                samples_keep.append((ntype, label, sd_iso, (sentence or "")[:60] + " [no-evidence]"))
            continue
        if sd_iso in chat_dates:
            downgrade += 1
            if len(samples_downgrade) < 10:
                samples_downgrade.append((ntype, label, sd_iso, (sentence or "")[:60]))
        else:
            kept += 1
            if len(samples_keep) < 10:
                samples_keep.append((ntype, label, sd_iso, (sentence or "")[:60]))
    return {
        "total_actuals": total,
        "kept": kept,
        "downgrade": downgrade,
        "no_evidence": no_evidence,
        "samples_keep": samples_keep,
        "samples_downgrade": samples_downgrade,
    }


def commit_downgrades(con: sqlite3.Connection) -> int:
    cur = con.cursor()
    rows = cur.execute(
        """
        SELECT id, start_date
        FROM kg_node_metadata
        WHERE node_type IN ('State', 'Event', 'Goal')
          AND start_date IS NOT NULL
          AND lower(coalesce(start_date_confidence, '')) = 'actual'
        """
    ).fetchall()
    to_downgrade = []
    for nid, start_date in rows:
        sd_iso = _start_date_iso(start_date)
        chat_dates = _evidence_chat_dates(con, nid)
        if not chat_dates:
            continue
        if sd_iso in chat_dates:
            to_downgrade.append(nid)
    if not to_downgrade:
        return 0
    cur.execute("BEGIN")
    try:
        # Chunked UPDATE; SQLite IN-list size limit is 32k parameters by default.
        BATCH = 500
        for i in range(0, len(to_downgrade), BATCH):
            chunk = to_downgrade[i:i + BATCH]
            placeholders = ",".join(["?"] * len(chunk))
            cur.execute(
                f"""
                UPDATE kg_node_metadata
                SET start_date_confidence = 'inferred',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id IN ({placeholders})
                """,
                chunk,
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    return len(to_downgrade)


def print_audit(report: dict) -> None:
    print(f"  total actuals: {report['total_actuals']}")
    print(f"  keep (date != chat date — likely past-event recall): {report['kept']}")
    print(f"  downgrade → inferred (start_date == chat date — chat stamp): {report['downgrade']}")
    print(f"    of which had no evidence rows (kept conservatively): {report['no_evidence']}")
    if report["samples_keep"]:
        print()
        print("  sample KEEPS (these stay 'actual'):")
        for ntype, label, sd, sent in report["samples_keep"]:
            print(f"    {sd}  {ntype:<6} {(label or '')[:32]:<32}  {sent!r}")
    if report["samples_downgrade"]:
        print()
        print("  sample DOWNGRADES (these become 'inferred'):")
        for ntype, label, sd, sent in report["samples_downgrade"]:
            print(f"    {sd}  {ntype:<6} {(label or '')[:32]:<32}  {sent!r}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--commit", action="store_true")
    p.add_argument("--yes-write", action="store_true",
                   help="Required to actually mutate emi.db in --commit mode")
    args = p.parse_args()

    if not (args.audit or args.commit):
        args.audit = True

    con = sqlite3.connect(DB_PATH)
    try:
        report = audit(con)
        print_audit(report)
        if args.audit:
            return

        if not args.yes_write:
            sys.exit("REFUSED: pass --yes-write to actually mutate emi.db")
        if not os.path.exists(BACKUP_PATH):
            sys.exit(f"REFUSED: no backup at {BACKUP_PATH}")
        age = time.time() - os.path.getmtime(BACKUP_PATH)
        if age > BACKUP_FRESH_SECONDS:
            sys.exit(f"REFUSED: emi.db.bak is {age/60:.0f} min old (must be < 60)")
        print(f"\nbackup OK ({age:.0f}s old)")
        print(f"\ndowngrading {report['downgrade']} rows: 'actual' → 'inferred' ...")
        n = commit_downgrades(con)
        print(f"  ✓ downgraded {n} rows")
        print("\nPost-migration audit:")
        print_audit(audit(con))
    finally:
        con.close()


if __name__ == "__main__":
    main()
