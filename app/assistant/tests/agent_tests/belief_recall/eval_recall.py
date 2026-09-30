"""Fixed-message eval for chat belief recall: does the belief that answers the message surface, does
the wrong one stay out, and how big is the block the chat gate is given. READ-ONLY (no surfacing log).

Runs each case two ways — the bare message, and the message expanded with the entities the chat
gate's detector finds (belief_engine.intake.recall.expand_query) — so a change to recall shows as
hits gained/lost and characters added/saved. Re-run after any recall change and compare.

    .venv\\Scripts\\python.exe -m app.assistant.tests.agent_tests.belief_recall.eval_recall
"""
from __future__ import annotations

import re
import sys

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from belief_engine.intake import recall

K = int(__import__("os").environ.get("RECALL_K", "6"))

# message -> (patterns some shown belief must match, patterns no shown belief may match). The cases name
# real people from the owner's catalog, so they live in a gitignored file beside this one.
_CASES_FILE = __import__("pathlib").Path(__file__).with_name("cases.private.json")
if not _CASES_FILE.is_file():
    raise SystemExit(f"missing {_CASES_FILE}: a JSON object message -> {{must: [regex], must_not: [regex]}}")
CASES = {m: (c["must"], c["must_not"]) for m, c in __import__("json").loads(_CASES_FILE.read_text(encoding="utf-8")).items()}


def run(message: str, expanded: bool, names_people: list[str]) -> tuple[list[dict], str]:
    names = [n for n in names_people if n.lower() in message.lower()]
    query = message
    if expanded:
        query, entities = recall.expand_query(message)
        names += [e for e in entities if e not in names]
    items = recall.recall(query, names=names, k=K)
    return items, recall.format_for_prompt(items)


def main() -> int:
    from app.assistant.ServiceLocator.service_locator import DI
    from belief_engine.intake import agents
    people = [p["name"] for p in (DI.resource_manager.get_resource(scope_context=agents.scope(),
              resource_id="resource_user_data") or {}).get("important_people") or [] if p.get("name")]
    totals = {False: [0, 0, 0], True: [0, 0, 0]}   # passes, chars, beliefs
    for message, (must, must_not) in CASES.items():
        print("=" * 100)
        print(f"MESSAGE: {message}")
        for expanded in (False, True):
            items, block = run(message, expanded, people)
            text = [b["statement"] for b in items]
            missing = [p for p in must if not any(re.search(p, t, re.I) for t in text)]
            wrong = [t for p in must_not for t in text if re.search(p, t, re.I)]
            ok = not missing and not wrong
            t = totals[expanded]
            t[0] += ok; t[1] += len(block); t[2] += len(items)
            label = "expanded" if expanded else "plain   "
            print(f"  {label} {'PASS' if ok else 'FAIL'}  {len(items):2} beliefs  {len(block):5} chars"
                  + (f"  missing {missing}" if missing else "") + (f"  wrong: {wrong[:2]}" if wrong else ""))
            for b in items:
                print(f"      {b['relevance']:.2f} {b['statement'][:120]}")
    n = len(CASES)
    for expanded in (False, True):
        p, c, b = totals[expanded]
        print(f"{'expanded' if expanded else 'plain   '}: {p}/{n} pass, {b} beliefs, {c} chars total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
