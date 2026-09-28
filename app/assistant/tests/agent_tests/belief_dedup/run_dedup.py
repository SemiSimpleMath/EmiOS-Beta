"""Replay extracted atoms through belief_engine::belief_dedup into an in-memory belief list. READ-ONLY.

Takes saved belief_extractor runs (scratch/extractor_runs/<stamp>_<date>.json, the newest per date,
trial 1), walks the dates in order, and checks every atom against the beliefs accumulated so far.
Candidates are shown all at once, ordered by embedding similarity (the ranking never drops one).
Code applies each verdict to the in-memory list:
    same        -> the atom's sources are added to the target belief
    refines     -> the atom becomes a child of the target (of the target's parent if the target is a child)
    contradicts -> the atom's sources are added to the target as contradicting
    new         -> the atom becomes a new belief
Nothing touches the belief store. The final list and every decision are saved to scratch/dedup_runs/.

Run from repo root:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_dedup.run_dedup 2026-09-13 2026-09-14 ...
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message

_AGENT = "belief_engine::belief_dedup"


def _item_text(item: dict) -> str:
    return (item.get("said") or item.get("typed_reply") or item.get("button")
            or item.get("label") or item.get("prompt") or "")


def _load_atoms(date: str, runs_dir: Path) -> list:
    runs = sorted(runs_dir.glob(f"*_{date}.json"))
    if not runs:
        raise SystemExit(f"No saved run for {date} in {runs_dir}.")
    data = json.loads(runs[-1].read_text(encoding="utf-8"))
    if not data["runs"]:
        raise SystemExit(f"Run for {date} has no successful trial.")
    if "atoms" in data["runs"][0]:
        # Atomic-insight runs already carry dedup-ready atoms with restored sources.
        return data["runs"][0]["atoms"]
    items = {i["n"]: i for i in data["timeline_items"]}
    atoms = []
    for a in data["runs"][0]["result"]["atoms"]:
        sources = []
        for c in a["citations"]:
            it = items[c["item"]]
            src = {"date": date, "time": it.get("time"), "kind": c["evidence_kind"],
                   "relation": c["relation"], "text": _item_text(it)}
            if it.get("prompt"):
                # A reply means nothing without the question it answered ("None for now").
                src["in_reply_to"] = it.get("prompt_text") or it.get("prompt")
            sources.append(src)
        atoms.append({"statement": a["statement"], "kind": a["kind"], "date": date, "sources": sources})
    return atoms


def _ordered(beliefs: list, statement: str, embed) -> list:
    """Every belief, closest in meaning first. Ranking only; nothing is dropped."""
    if not beliefs:
        return []
    import numpy as np
    vecs = np.asarray(embed([statement] + [b["statement"] for b in beliefs]), dtype=float)
    vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
    sims = vecs[1:] @ vecs[0]
    return [beliefs[i] for i in np.argsort(-sims)]


def _candidate_view(b: dict, by_id: dict) -> dict:
    view = {"id": b["id"], "statement": b["statement"], "kind": b["kind"], "sources": b["sources"]}
    if b.get("parent"):
        view["refines"] = by_id[b["parent"]]["statement"]
    kids = [by_id[c]["statement"] for c in b.get("children", [])]
    if kids:
        view["refined_by"] = kids
    return view


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dates", nargs="+")
    parser.add_argument("--runs-dir", default="scratch/extractor_runs",
                        help="where the saved atom runs live (scratch/atomic_insight_runs for atomic insights)")
    args = parser.parse_args()
    runs_dir = get_repo_root() / args.runs_dir

    scope = load_scope_for_source(
        kind="subsystem", source_id="belief_engine", actor_id="belief_dedup_test",
        identity_overrides={"owner_id": "belief_engine", "surface": "pipeline",
                            "scope_id": "scope::belief_engine::belief_dedup_test"})
    agent = DI.agent_factory.create_agent(_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {_AGENT!r}")
    from app.assistant.embeddings.embedder import embed_texts

    beliefs, by_id, decisions, problems = [], {}, [], []
    for date in sorted(args.dates):
        atoms = _load_atoms(date, runs_dir)
        print("=" * 100)
        print(f"{date}: {len(atoms)} atoms, {len(beliefs)} beliefs held")
        for atom in atoms:
            ordered = _ordered(beliefs, atom["statement"], embed_texts)
            new_view = {"statement": atom["statement"], "kind": atom["kind"], "sources": atom["sources"]}
            if not ordered:
                verdict = {"verdict": "new", "target": None, "reasoning": "No beliefs held yet."}
            else:
                result = agent.action_handler(Message(
                    agent_input={"new_belief": new_view,
                                 "existing_beliefs": [_candidate_view(b, by_id) for b in ordered]},
                    scope_context=scope))
                verdict = getattr(result, "data", None) or {}
            v, target = verdict.get("verdict"), (verdict.get("target") or "").strip() or None
            if v != "new" and target not in by_id:
                problems.append(f"{date}: verdict {v} with unknown target {target!r} for {atom['statement'][:60]}")
                print(f"  !! INVALID target {target!r}; treating as new")
                v, target = "new", None
            if v == "new":
                bid = f"B{len(beliefs) + 1}"
                b = {"id": bid, "statement": atom["statement"], "kind": atom["kind"],
                     "sources": list(atom["sources"]), "parent": None, "children": []}
                beliefs.append(b); by_id[bid] = b
            elif v in ("same", "contradicts"):
                srcs = atom["sources"] if v == "same" else [{**s, "relation": "contradict"} for s in atom["sources"]]
                by_id[target]["sources"].extend(srcs)
            elif v == "refines":
                parent = by_id[target]["parent"] or target      # one level only
                bid = f"B{len(beliefs) + 1}"
                b = {"id": bid, "statement": atom["statement"], "kind": atom["kind"],
                     "sources": list(atom["sources"]), "parent": parent, "children": []}
                beliefs.append(b); by_id[bid] = b; by_id[parent]["children"].append(bid)
            else:
                problems.append(f"{date}: invalid verdict {v!r}")
                continue
            where = f" -> {target}: {by_id[target]['statement'][:70]}" if target else ""
            print(f"  [{v:11}] {atom['statement']}{where}")
            print(f"               {verdict.get('reasoning')}")
            decisions.append({"date": date, "atom": atom, **verdict})

    print("\n" + "=" * 100 + "\nFINAL BELIEFS")
    for b in beliefs:
        if b["parent"]:
            continue
        dates = sorted({s["date"] for s in b["sources"]})
        print(f"  {b['id']} [{b['kind']}] {b['statement']}")
        print(f"       {len(b['sources'])} source(s) on {', '.join(dates)}")
        for c in b["children"]:
            ch = by_id[c]
            print(f"     └ {ch['id']} [{ch['kind']}] {ch['statement']}  ({len(ch['sources'])} source(s))")
    counts = collections.Counter(d["verdict"] for d in decisions)
    print(f"\n  {sum(counts.values())} atoms -> {len([b for b in beliefs if not b['parent']])} beliefs "
          f"(+{len([b for b in beliefs if b['parent']])} children); verdicts {dict(counts)}")
    print("  CHECKS: " + ("all passed" if not problems else ""))
    for p in problems:
        print(f"    FAIL {p}")

    out = get_repo_root() / "scratch" / "dedup_runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({"dates": args.dates, "decisions": decisions, "beliefs": beliefs},
                               indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nsaved {path}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
