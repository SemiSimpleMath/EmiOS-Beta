"""Run the PROTOTYPE atomic insight writer over past days, side by side with the live insights. READ-ONLY.

For each date: numbers that day's timeline items (timeline_merged.json), calls
daily_timeline_insights_atomic, and prints the day's CURRENT insights (resource_daily_insights.json)
next to the atomic ones, each atomic insight with the exact items it cites. Runs the mechanical
checks. Nothing in day_context/ or the belief store is written.

Each run is saved under scratch/atomic_insight_runs/<stamp>_<date>.json; its `atoms` field is what
belief_dedup/run_dedup.py reads with --runs-dir scratch/atomic_insight_runs.

Run from repo root:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.daily_timeline_insights_atomic.run_atomic_insights 2026-09-24 [...] [--trials N]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.tests.agent_tests.belief_extractor.run_extractor import _day_dir, _timeline_items
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message

_AGENT = "daily_timeline_insights_atomic"
_DIRECTIVE = re.compile(r"\b(should|must|do not|don't|avoid)\b", re.I)


def _item_text(item: dict) -> str:
    return (item.get("said") or item.get("typed_reply") or item.get("button")
            or item.get("label") or item.get("prompt") or "")


def _checks(result, n_items, allowed_tags):
    problems = []
    for ins in result.get("actionable_information") or []:
        refs = [s["item"] for s in ins.get("sources") or []]
        bad = [r for r in refs if not 1 <= r <= n_items]
        if bad:
            problems.append(f"cites items outside 1..{n_items}: {bad} — {ins['fact_summary'][:60]}")
        if len(set(refs)) != len(refs):
            problems.append(f"cites an item twice — {ins['fact_summary'][:60]}")
        off = [t for t in ins.get("tags") or [] if t not in allowed_tags]
        if off:
            problems.append(f"tags outside vocabulary {off} — {ins['fact_summary'][:60]}")
    return problems


def _atoms_for_dedup(date, result, items):
    """Same shape run_dedup.py builds from extractor runs: statement, kind, restored sources."""
    by_n = {i["n"]: i for i in items}
    atoms = []
    for ins in result.get("actionable_information") or []:
        sources = []
        for s in ins["sources"]:
            it = by_n[s["item"]]
            src = {"date": date, "time": it.get("time"), "kind": s["evidence_kind"],
                   "relation": "support", "text": _item_text(it)}
            if it.get("prompt"):
                src["in_reply_to"] = it.get("prompt_text") or it.get("prompt")
            sources.append(src)
        atoms.append({"statement": ins["fact_summary"], "kind": ins["kind"], "date": date,
                      "sources": sources})
    return atoms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dates", nargs="+")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()

    from belief_engine.config import list_all_tags
    allowed_tags = list_all_tags(only_enabled=True)
    from app.assistant.scope.loader import load_scope_for_source
    # The daily_insights pipeline has no scope.yaml (the live writer resolves no resources); the
    # prototype needs one for resource_user_data, so it borrows the belief engine's, as the other
    # belief harnesses do. Wiring the atomic writer in for real needs a daily_insights scope.
    scope = load_scope_for_source(
        kind="subsystem", source_id="belief_engine", actor_id="daily_timeline_insights_atomic_test",
        identity_overrides={"owner_id": "belief_engine", "surface": "pipeline",
                            "scope_id": "scope::belief_engine::atomic_insights_test"})
    agent = DI.agent_factory.create_agent(_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {_AGENT!r}")
    out_dir = get_repo_root() / "scratch" / "atomic_insight_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    failed, summary = 0, []
    for date in args.dates:
        day = _day_dir(date)
        items = _timeline_items(json.loads((day / "timeline_merged.json").read_text(encoding="utf-8")).get("timeline") or [])
        current_path = day / "resource_daily_insights.json"
        current = json.loads(current_path.read_text(encoding="utf-8")).get("actionable_information", []) if current_path.exists() else []
        by_n = {i["n"]: i for i in items}
        print("=" * 100)
        print(f"{date}: {len(items)} timeline items")
        print("  CURRENT insights:")
        for c in current:
            print(f"    * {c.get('fact_summary')}")
        runs = []
        for trial in range(1, args.trials + 1):
            print(f"  --- ATOMIC, trial {trial}/{args.trials} ---")
            try:
                result = agent.action_handler(Message(
                    agent_input={"date": date, "timeline_items": items, "memory_available_tags": allowed_tags},
                    scope_context=scope))
                data = getattr(result, "data", None)
                if not isinstance(data, dict) or "actionable_information" not in data:
                    raise RuntimeError(f"no actionable_information: {result!r}")
            except Exception as exc:
                print(f"  ERROR {type(exc).__name__}: {exc}")
                failed += 1
                summary.append((date, trial, "error", [str(exc)]))
                continue
            problems = _checks(data, len(items), set(allowed_tags))
            for ins in data["actionable_information"]:
                flag = "!!" if _DIRECTIVE.search(ins["fact_summary"]) else "->"
                print(f"    {flag} [{ins['kind']} / {ins['temporal_scope']}] {ins['fact_summary']}")
                for s in ins["sources"]:
                    it = by_n.get(s["item"], {})
                    print(f"         #{s['item']} {s['evidence_kind']} {it.get('time')} {it.get('type')}: {_item_text(it)}")
            for p in problems:
                print(f"    FAIL {p}")
            failed += bool(problems)
            runs.append({"trial": trial, "result": data, "problems": problems,
                         "atoms": _atoms_for_dedup(date, data, items)})
            summary.append((date, trial, f"{len(current)} current -> {len(data['actionable_information'])} atomic", problems))
        (out_dir / f"{stamp}_{date}.json").write_text(json.dumps(
            {"date": date, "timeline_items": items, "current_insights": current, "runs": runs},
            indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 100 + "\nSUMMARY")
    for date, trial, what, problems in summary:
        print(f"  {date} trial {trial}: {what}  {'checks ok' if not problems else problems}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
