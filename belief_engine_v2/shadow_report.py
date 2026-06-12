"""CLI: print the belief shadow-run comparison.

Usage:
    .venv/Scripts/python.exe -m belief_engine_v2.shadow_report
    .venv/Scripts/python.exe -m belief_engine_v2.shadow_report --days 14
"""
from __future__ import annotations

import argparse

from belief_engine_v2.shadow import diff_run, load_shadow_runs, v2_activity


def main() -> None:
    parser = argparse.ArgumentParser(description="Belief v1-vs-v2 shadow-run report.")
    parser.add_argument("--days", type=float, default=7.0)
    args = parser.parse_args()

    runs = load_shadow_runs(days=args.days)
    print("=" * 72)
    print(f"BELIEF SHADOW REPORT — last {args.days:g} days, {len(runs)} recorded run(s)")
    print("=" * 72)

    for run in runs:
        d = diff_run(run)
        print(f"\n[{d['recorded_at']}] agent={d['agent']}  "
              f"v2={d['v2_count']} items, v1={d['v1_count']} items "
              f"(diff is APPROXIMATE; phrasing differs between lanes)")
        if d["only_v2"]:
            print("  only in v2:")
            for s in d["only_v2"]:
                print(f"    + {s[:110]}")
        if d["only_v1"]:
            print("  only in v1:")
            for s in d["only_v1"]:
                print(f"    - {s[:110]}")
        if not d["only_v2"] and not d["only_v1"]:
            print("  (lanes agree)")

    print("\n" + "=" * 72)
    print("V2 ENGINE ACTIVITY")
    print("=" * 72)
    act = v2_activity(days=args.days)
    print(f"observations folded: {act['observations_folded']}")
    print(f"resolver method mix: {act['resolver_method_mix']}")
    print(f"belief totals by status: {act['belief_totals_by_status']}")
    print(f"\nminted this window ({len(act['beliefs_minted'])}):")
    for b in act["beliefs_minted"][:15]:
        print(f"  + [{b['kind']}] {str(b['statement_nl'])[:100]}")
    print(f"\nreinforced this window ({len(act['beliefs_reinforced'])}):")
    for b in act["beliefs_reinforced"][:15]:
        print(f"  ~ [{b['obs_count']}x] {str(b['statement_nl'])[:100]}")
    if act["contested"]:
        print(f"\ncontested ({len(act['contested'])}):")
        for b in act["contested"]:
            print(f"  ! (+{b['support']:.1f}/-{b['contradiction']:.1f}) {str(b['statement_nl'])[:95]}")
    if act["merges"]:
        print(f"\nmerges ({len(act['merges'])}):")
        for m in act["merges"]:
            print(f"  {m['belief_id'][:8]} -> {m['merged_into'][:8]} ({m['method']})")


if __name__ == "__main__":
    main()
