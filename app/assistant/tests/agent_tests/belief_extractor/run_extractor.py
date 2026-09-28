"""Run belief_engine::belief_extractor over past days. READ-ONLY.

For each date, reads that day's resource_daily_insights.json and timeline_merged.json from
day_context/, numbers every timeline item, calls the extractor, prints the atoms with the exact
items they cite, and runs the checks a future writer would enforce. Nothing touches the belief
store. Each run is saved as JSON under scratch/extractor_runs/ (gitignored).

Run from repo root:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_extractor.run_extractor 2026-09-24 [2026-09-25 ...] [--trials N]
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message

_AGENT = "belief_engine::belief_extractor"
# Wording that addresses the assistant or prescribes action. Flagged for review, never rejected.
_DIRECTIVE = re.compile(r"\b(should|must|the assistant|assistant|do not|don't|avoid)\b", re.I)


def _day_dir(date: str) -> Path:
    return get_repo_root() / "day_context" / date[:4] / date[5:7] / date


def _timeline_items(timeline: list) -> list:
    """Number every item and keep what a reader needs; the model cites numbers only."""
    items = []
    for n, e in enumerate(timeline, start=1):
        kind = e.get("type")
        if kind == "chat":
            item = {"n": n, "type": "chat message", "time": e.get("start_time_local"),
                    "said": e.get("message")}
        elif kind == "ticket":
            item = {"n": n, "type": "prompt / ticket", "time": e.get("start_time_local"),
                    "prompt": e.get("title"), "prompt_text": e.get("message"),
                    "outcome": e.get("state"), "button": e.get("user_action"),
                    "typed_reply": e.get("user_text")}
        else:
            end = e.get("end_time_local")
            item = {"n": n, "type": kind or "context",
                    "time": f"{e.get('start_time_local')}–{end}" if end else e.get("start_time_local"),
                    "label": e.get("label"), "basis": e.get("evidence")}
        items.append(item)
    return items


def _insights(data: dict) -> list:
    """The user-facing insight text only — not the guidance fields."""
    return [{"n": n, "insight": a.get("fact_summary"), "evidence_noted": a.get("evidence") or [],
             "scope": a.get("temporal_scope")}
            for n, a in enumerate(data.get("actionable_information") or [], start=1)]


def _checks(insights, items, result):
    problems = []
    n_ins, n_items = len(insights), len(items)
    covered = set()
    for a in result.get("atoms") or []:
        if not 1 <= a["insight_ref"] <= n_ins:
            problems.append(f"atom cites insight {a['insight_ref']} outside 1..{n_ins}")
        covered.add(a["insight_ref"])
        refs = [c["item"] for c in a.get("citations") or []]
        bad = [r for r in refs if not 1 <= r <= n_items]
        if bad:
            problems.append(f"atom cites timeline items outside 1..{n_items}: {bad}")
        if len(set(refs)) != len(refs):
            problems.append(f"atom cites an item twice: {a['statement'][:60]}")
    for w in result.get("insights_without_beliefs") or []:
        if w["insight_ref"] in covered:
            problems.append(f"insight {w['insight_ref']} is both used and reported without belief")
        covered.add(w["insight_ref"])
    missing = set(range(1, n_ins + 1)) - covered
    if missing:
        problems.append(f"insights neither used nor explained: {sorted(missing)}")
    return problems


def _print(date, insights, items, result, problems):
    by_n = {i["n"]: i for i in items}
    print(f"reasoning: {result.get('reasoning')}\n")
    for ins in insights:
        print(f"  INSIGHT {ins['n']}: {ins['insight']}")
        for a in [a for a in result.get("atoms") or [] if a["insight_ref"] == ins["n"]]:
            flag = "!!" if _DIRECTIVE.search(a["statement"]) else "->"
            print(f"    {flag} [{a['kind']}] {a['statement']}")
            for c in a["citations"]:
                it = by_n.get(c["item"], {})
                text = it.get("said") or it.get("typed_reply") or it.get("button") or it.get("label") or it.get("prompt") or ""
                print(f"         #{c['item']} {c['evidence_kind']}/{c['relation']} {it.get('time')} {it.get('type')}: {text}")
        for w in [w for w in result.get("insights_without_beliefs") or [] if w["insight_ref"] == ins["n"]]:
            print(f"    -- no belief: {w['reason']}")
        print()
    print("  CHECKS: " + ("all passed" if not problems else ""))
    for p in problems:
        print(f"    FAIL {p}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dates", nargs="+")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()

    scope = load_scope_for_source(
        kind="subsystem", source_id="belief_engine", actor_id="belief_extractor_test",
        identity_overrides={"owner_id": "belief_engine", "surface": "pipeline",
                            "scope_id": "scope::belief_engine::belief_extractor_test"})
    agent = DI.agent_factory.create_agent(_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {_AGENT!r}")
    out_dir = get_repo_root() / "scratch" / "extractor_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    failed = 0
    summary = []
    for date in args.dates:
        day = _day_dir(date)
        insights = _insights(json.loads((day / "resource_daily_insights.json").read_text(encoding="utf-8")))
        items = _timeline_items(json.loads((day / "timeline_merged.json").read_text(encoding="utf-8")).get("timeline") or [])
        print("=" * 100)
        print(f"{date}: {len(insights)} insights, {len(items)} timeline items "
              f"({dict(collections.Counter(i['type'] for i in items))})")
        runs = []
        for trial in range(1, args.trials + 1):
            print(f"--- trial {trial}/{args.trials} ---")
            try:
                result = agent.action_handler(Message(
                    agent_input={"date": date, "insights": insights, "timeline_items": items},
                    scope_context=scope))
                data = getattr(result, "data", None)
                if not isinstance(data, dict) or "atoms" not in data:
                    raise RuntimeError(f"extractor returned no atoms field: {result!r}")
            except Exception as exc:
                print(f"ERROR extractor call failed: {type(exc).__name__}: {exc}")
                failed += 1
                summary.append((date, trial, "error", 0, [str(exc)]))
                continue
            problems = _checks(insights, items, data)
            failed += bool(problems)
            _print(date, insights, items, data, problems)
            runs.append({"trial": trial, "result": data, "problems": problems})
            flagged = sum(bool(_DIRECTIVE.search(a["statement"])) for a in data["atoms"])
            summary.append((date, trial, f"{len(data['atoms'])} atoms, {flagged} flagged", len(insights), problems))
        path = out_dir / f"{stamp}_{date}.json"
        path.write_text(json.dumps({"date": date, "insights": insights, "timeline_items": items, "runs": runs},
                                   indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nsaved {path}")

    print("\n" + "=" * 100 + "\nSUMMARY")
    for date, trial, what, n_ins, problems in summary:
        print(f"  {date} trial {trial}: {n_ins} insights -> {what}  {'checks ok' if not problems else problems}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
