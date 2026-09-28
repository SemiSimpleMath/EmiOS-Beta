"""Replay stored daily timelines through the intake into a SEPARATE scratch store.

    extract (per day, in parallel, cached)  ->  judge (day by day, in date order)

Reads day_context/ and the agents; writes only the scratch directory. Never opens the app's belief
tables. Resumable: saved extractions are reused and finished days are skipped.

Run from repo root:
    .venv\\Scripts\\python.exe -m belief_engine.intake.replay --from 2026-02-10 --days 10
    .venv\\Scripts\\python.exe -m belief_engine.intake.replay --from 2026-02-10 --to 2026-09-25
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end")
    parser.add_argument("--days", type=int, help="process at most this many days from --from")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--out", default="scratch/belief_replay")
    args = parser.parse_args()

    sys.path.insert(0, ".")
    from app.assistant.tests.test_setup import initialize_services
    initialize_services()
    from app.assistant.embeddings.embedder import embed_texts
    from app.assistant.utils.path_utils import get_repo_root
    from belief_engine.intake import agents, day_items
    from belief_engine.intake.run import judge_day
    from belief_engine.intake.store import IntakeStore, sqlite_file

    out = get_repo_root() / args.out
    extract_dir = out / "extract"
    extract_dir.mkdir(parents=True, exist_ok=True)
    store = IntakeStore(sqlite_file(out / "belief_replay.db"))
    scope_ctx = agents.scope()

    days = [d for d in day_items.available_days() if d >= args.start and (not args.end or d <= args.end)]
    if args.days:
        days = days[:args.days]
    print(f"{len(days)} days: {days[0]} .. {days[-1]}")

    # --- Pass 1: extraction, independent per day, in parallel ---------------------------------
    todo = [d for d in days if not (extract_dir / f"{d}.json").exists()]
    print(f"extracting {len(todo)} day(s) with {args.workers} workers")
    t0 = time.monotonic()

    def run_extract(day):
        ins = day_items.insights(day)
        items, provenance = day_items.timeline_items(day)
        try:
            atoms = agents.extract_day(day, ins, items, provenance, scope_ctx)
            record = {"day": day, "insights": len(ins), "atoms": atoms}
        except Exception as exc:
            record = {"day": day, "insights": len(ins), "error": f"{type(exc).__name__}: {exc}"}
        (extract_dir / f"{day}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for fut in as_completed([pool.submit(run_extract, d) for d in todo]):
            r = fut.result()
            print(f"  extracted {r['day']}: {r['insights']} insights -> "
                  f"{len(r['atoms']) if 'atoms' in r else 'ERROR ' + r['error']}")
    print(f"extraction pass: {time.monotonic() - t0:.0f}s")

    # --- Pass 2: judge, strictly in date order -----------------------------------------------
    done = store.days_done()
    totals: dict[str, int] = {}
    failures = []
    t1 = time.monotonic()
    for day in days:
        if day in done:
            continue
        record = json.loads((extract_dir / f"{day}.json").read_text(encoding="utf-8"))
        if "error" in record:
            store.mark_day(day, "failed", 0, record["error"])
            failures.append((day, record["error"]))
            print(f"  {day}: SKIPPED — extraction failed: {record['error']}")
            continue
        atoms = record["atoms"]
        try:
            counts = judge_day(day, atoms, store, scope_ctx, embed_texts)
            for k, n in counts.items():
                totals[k] = totals.get(k, 0) + n
            store.mark_day(day, "done", len(atoms))
        except Exception as exc:
            # Later days depend on this one's beliefs; stop rather than build on a gap.
            store.mark_day(day, "failed", len(atoms), f"{type(exc).__name__}: {exc}")
            print(f"  {day}: FAILED while judging — stopping: {type(exc).__name__}: {exc}")
            failures.append((day, str(exc)))
            break
    print(f"dedup pass: {time.monotonic() - t1:.0f}s")

    # --- Report -----------------------------------------------------------------------------
    beliefs = store.beliefs()
    top = [b for b in beliefs if not b["parent_id"]]
    print(f"\nverdicts this run: {totals}")
    print(f"store: {len(top)} beliefs + {len(beliefs) - len(top)} refinements, "
          f"{sum(len(b['sources']) for b in beliefs)} evidence rows")
    for day, err in failures:
        print(f"  FAILED {day}: {err}")
    export = out / "beliefs.csv"
    with export.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "refines", "statement", "kind", "sources", "days", "first_day", "last_day"])
        for b in beliefs:
            ds = sorted({s["day"] for s in b["sources"]})
            w.writerow([b["id"], b["parent_id"] or "", b["statement"], b["kind"], len(b["sources"]),
                        len(ds), ds[0] if ds else "", ds[-1] if ds else ""])
    print(f"exported {export}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
