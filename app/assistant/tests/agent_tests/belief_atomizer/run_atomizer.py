"""Run belief_engine::belief_atomizer against real stored beliefs. READ-ONLY.

Loads each named belief and every real observation in its merge lineage (the same
`observations()` the matcher uses: bookkeeping rows excluded, identical observations
collapsed), calls the atomizer, prints the atoms, and runs the checks a future writer
will have to enforce. Nothing is written to the belief store.

Each run is also saved as JSON under scratch/atomizer_runs/ (gitignored) so prompt
revisions can be compared on the same beliefs.

Run from repo root:
    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_atomizer.run_atomizer <belief_key> [<belief_key> ...] [--trials N]

--trials runs the same belief N times. Split variance (the same belief carved
differently on different runs) is the main risk of atomizing, so look at it directly.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import Message
from belief_engine.db.paths import belief_db_path
from belief_engine.matching.context import NON_OBSERVATION_SOURCES, lineage, observations

_AGENT = "belief_engine::belief_atomizer"
_BELIEF_FIELDS = ("belief_key", "statement", "kind", "scope", "domain", "confidence",
                  "conditions", "status", "created_at", "first_observed", "last_confirmed")
_EVIDENCE_FIELDS = ("source_type", "source_date", "signal_type", "valence", "weight",
                    "summary", "raw_text")


def _load(conn, belief_key):
    """The belief, its numbered observations, and how many bookkeeping rows were left out."""
    row = conn.execute("SELECT * FROM user_beliefs WHERE belief_key=?", (belief_key,)).fetchone()
    if row is None:
        raise SystemExit(f"No active belief with key {belief_key!r}")
    belief = dict(row)
    records, all_evidence = lineage(conn, belief["id"])
    key_of = {r["id"]: r["belief_key"] for r in records}
    obs = sorted(observations(conn, belief["id"]),
                 key=lambda e: (e.get("source_date") or "", e["id"]))
    # Identical unlinked rows copied onto many merged predecessors (defect 2.7) are shown once,
    # with how many copies exist. This is presentation only; the store is not changed.
    groups = {}
    for ev in obs:
        content = tuple(ev.get(k) for k in _EVIDENCE_FIELDS if k != "weight")
        groups.setdefault(content, []).append(ev)
    numbered = []
    for i, rows in enumerate(groups.values(), start=1):
        ev = rows[0]
        item = {"ref": i, **{k: ev.get(k) for k in _EVIDENCE_FIELDS}}
        if len(rows) > 1:
            item["stored_copies"] = len(rows)
        on = sorted({key_of.get(r["belief_id"], r["belief_id"]) for r in rows if r["belief_id"] != belief["id"]})
        if on:
            # Reached through a merge: say which absorbed belief(s) it was recorded against.
            item["recorded_on_beliefs"] = on
        numbered.append((item, [r["id"] for r in rows]))
    bookkeeping = sum(1 for e in all_evidence if e["source_type"] in NON_OBSERVATION_SOURCES)
    evidence = [item for item, _ in numbered]
    evidence_ids = {item["ref"]: ids for item, ids in numbered}
    return belief, evidence, evidence_ids, len(obs), bookkeeping, len(records) - 1


def _checks(belief, evidence, result, existing_keys):
    """What the writer will refuse. Reported here, never enforced by raising."""
    problems = []
    atoms = result.get("atoms") or []
    n = len(evidence)
    primaries = [a for a in atoms if a.get("is_primary")]
    if len(primaries) != 1:
        problems.append(f"{len(primaries)} primary atoms; exactly one required")
    elif primaries[0]["belief_key"] != belief["belief_key"]:
        problems.append(f"primary atom key {primaries[0]['belief_key']!r} is not the original key")
    for a in atoms:
        if not a.get("is_primary") and a["belief_key"] == belief["belief_key"]:
            problems.append(f"non-primary atom reuses the original key")
    keys = [a["belief_key"] for a in atoms]
    if len(set(keys)) != len(keys):
        problems.append(f"duplicate atom keys: {keys}")
    for a in atoms:
        if not a.get("is_primary") and a["belief_key"] in existing_keys:
            problems.append(f"new key {a['belief_key']!r} collides with an existing belief")
    verdict = result.get("verdict")
    if verdict == "already_atomic" and len(atoms) != 1:
        problems.append(f"verdict already_atomic but {len(atoms)} atoms")
    if verdict == "split" and len(atoms) < 2:
        problems.append("verdict split but only one atom")

    cited = set()
    for a in atoms:
        refs = a.get("evidence_refs") or []
        rel_refs = [r["evidence_ref"] for r in a.get("evidence_relations") or []]
        bad = [r for r in refs + rel_refs if not 1 <= r <= n]
        if bad:
            problems.append(f"{a['belief_key']}: evidence refs out of range 1..{n}: {sorted(set(bad))}")
        if len(set(refs)) != len(refs):
            problems.append(f"{a['belief_key']}: evidence ref cited twice")
        if sorted(refs) != sorted(rel_refs):
            problems.append(f"{a['belief_key']}: evidence_refs {sorted(refs)} and relations "
                            f"{sorted(rel_refs)} do not match one to one")
        cited.update(refs)
    unattached = set(result.get("unattached_evidence_refs") or [])
    if unattached & cited:
        problems.append(f"refs both cited and reported unattached: {sorted(unattached & cited)}")
    silent = set(range(1, n + 1)) - cited - unattached
    if silent:
        problems.append(f"refs neither cited nor reported unattached: {sorted(silent)}")
    return problems


def _print_run(belief, evidence, result, problems):
    print(f"verdict: {result.get('verdict')}   atoms: {len(result.get('atoms') or [])}")
    print(f"reasoning: {result.get('reasoning')}\n")
    for a in result.get("atoms") or []:
        mark = "PRIMARY " if a.get("is_primary") else ""
        print(f"  [{mark}{a['belief_key']}]  {a['kind']} / {a['scope']} / {a['confidence']}")
        print(f"    {a['statement']}")
        if a.get("conditions_json"):
            print(f"    conditions: {a['conditions_json']}")
        for r in a.get("evidence_relations") or []:
            print(f"    ev {r['evidence_ref']} {r['valence']}: {r['reasoning']}")
        print(f"    why: {a['reasoning']}\n")
    if result.get("unattached_evidence_refs"):
        print(f"  unattached evidence: {result['unattached_evidence_refs']}")
    print("  CHECKS: " + ("all passed" if not problems else ""))
    for p in problems:
        print(f"    FAIL {p}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("belief_keys", nargs="*")
    parser.add_argument("--keys-file", help="file with one belief_key per line")
    parser.add_argument("--trials", type=int, default=1)
    args = parser.parse_args()
    keys = list(args.belief_keys)
    if args.keys_file:
        keys += [k.strip() for k in Path(args.keys_file).read_text(encoding="utf-8-sig").splitlines() if k.strip()]
    if not keys:
        parser.error("no belief keys given")

    scope = load_scope_for_source(
        kind="subsystem", source_id="belief_engine", actor_id="belief_atomizer_test",
        identity_overrides={"owner_id": "belief_engine", "surface": "pipeline",
                            "scope_id": "scope::belief_engine::belief_atomizer_test"})
    agent = DI.agent_factory.create_agent(_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {_AGENT!r}")

    out_dir = get_repo_root() / "scratch" / "atomizer_runs"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Read-only at the SQLite level: this harness cannot write to the belief store.
    conn = sqlite3.connect(Path(belief_db_path()).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    summary = []
    try:
        existing_keys = {r[0] for r in conn.execute("SELECT belief_key FROM user_beliefs")}
        failed = 0
        for key in keys:
            try:
                belief, evidence, evidence_ids, n_rows, bookkeeping, predecessors = _load(conn, key)
            except SystemExit as exc:
                print(f"ERROR {exc}")
                summary.append((key, "missing", 0, ["not an active belief"]))
                failed += 1
                continue
            print("=" * 100)
            print(f"{key}")
            print(f"  {belief['kind']} / {belief['scope']} / {belief['confidence']} / "
                  f"{len((belief['statement'] or '').split())} words / locked={belief['locked']}")
            print(f"  {len(evidence)} distinct observations ({n_rows} stored rows), {bookkeeping} bookkeeping rows left out, "
                  f"{predecessors} merged predecessors")
            print(f"\n  {belief['statement']}\n")
            if belief["locked"]:
                print("  SKIPPED: owner-locked beliefs never reach the atomizer.")
                continue

            runs = []
            for trial in range(1, args.trials + 1):
                print(f"--- trial {trial}/{args.trials} ---")
                try:
                    result = agent.action_handler(Message(
                        agent_input={"belief": {k: belief.get(k) for k in _BELIEF_FIELDS},
                                     "evidence": evidence},
                        scope_context=scope))
                    data = getattr(result, "data", None)
                    if not isinstance(data, dict) or "atoms" not in data:
                        raise RuntimeError(f"atomizer returned no atoms: {result!r}")
                except Exception as exc:
                    # One failed call is reported and counted; the other beliefs still run.
                    print(f"ERROR atomizer call failed: {type(exc).__name__}: {exc}")
                    summary.append((key, "error", 0, [str(exc)]))
                    failed += 1
                    continue
                problems = _checks(belief, evidence, data, existing_keys)
                failed += bool(problems)
                _print_run(belief, evidence, data, problems)
                runs.append({"trial": trial, "result": data, "problems": problems})
                summary.append((key, data.get("verdict"), len(data.get("atoms") or []), problems))

            if args.trials > 1:
                print("--- carving across trials ---")
                for r in runs:
                    atoms = r["result"].get("atoms") or []
                    print(f"  trial {r['trial']}: {r['result'].get('verdict')} "
                          f"{[(a['belief_key'], a['kind']) for a in atoms]}")

            path = out_dir / f"{stamp}_{key}.json"
            path.write_text(json.dumps({"belief": belief, "evidence": evidence,
                                        "evidence_ids_by_ref": evidence_ids, "runs": runs},
                                       indent=2, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"\nsaved {path}")
    finally:
        conn.close()

    print("\n" + "=" * 100)
    print("SUMMARY")
    for key, verdict, n_atoms, problems in summary:
        print(f"  {verdict or '?':15} {n_atoms} atom(s)  {'checks ok' if not problems else f'{len(problems)} check failure(s)'}  {key}")
    verdicts = collections.Counter(v for _, v, _, _ in summary)
    print(f"  totals: {dict(verdicts)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
