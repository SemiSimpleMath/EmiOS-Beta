"""Run the intake for stored days: extract → dedup → apply (a contradiction is held for the owner), one day at a time.

Shared by the replay (scratch file, cached extractions) and shadow mode (the app DB's
belief_intake_* tables, nothing read by the assistant yet). Days are processed in date order because each
day's judgments rest on the beliefs the earlier days left; a day that fails is marked failed and
stops the run, and is retried the next time.

Backfill or catch up by hand, from the repo root:
    .venv\\Scripts\\python.exe -m belief_engine.intake.run --through 2026-09-26
"""
from __future__ import annotations

import argparse
import sys
import time

from app.assistant.utils.logging_config import get_logger

from belief_engine.intake import agents, day_items
from belief_engine.intake.propagate import contradiction_fanout
from belief_engine.intake.rank import ordered
from belief_engine.intake.store import IntakeStore

logger = get_logger(__name__)

APP_TABLE_PREFIX = "belief_intake_"


def judge_day(day: str, atoms: list[dict], store: IntakeStore, scope_ctx, embed_texts, log=print) -> dict:
    """Dedup and apply one day's atoms against the store (contradictions held for the owner). Returns verdict
    counts."""
    counts: dict[str, int] = {}
    vectors = embed_texts([a["statement"] for a in atoms]) if atoms else []
    for atom, vec in zip(atoms, vectors):
        judge_atom(day, atom, vec, store, scope_ctx, log, counts)
    return counts


def judge_atom(day: str, atom: dict, vec: list, store: IntakeStore, scope_ctx, log=print,
               counts: dict | None = None) -> str:
    """Dedup and apply one atom. Returns the id of the belief it landed on.

    An atom that contradicts held beliefs is not applied to them: it is kept as its own `pending` belief with
    every held belief it contradicts (the one dedup named, and the others the fan-out finds) and, for each, a
    drafted rewrite that reconciles the two (agents.draft_revision), for the owner to apply or decline in the
    morning review (owner, 2026-10-03). Until then the held beliefs stand as they were. Until 2026-10-03 a
    contradiction rewrote the held belief at once with the same call: right in content (intermittent fasting to
    calorie-reduced meals, four beliefs overnight) but with "until <date>" clauses, and once a misread (B198)."""
    counts = {} if counts is None else counts

    def tick(key):
        counts[key] = counts.get(key, 0) + 1

    verdict = agents.dedup(atom, day, ordered(store.beliefs(), vec), scope_ctx)
    tick(verdict["verdict"])
    if verdict["verdict"] != "contradicts":
        bid = store.apply(day, atom, vec, verdict)
        tgt = f" -> {verdict['target']}" if verdict.get("target") else ""
        log(f"  {day} [{verdict['verdict']:11}] {bid}{tgt}: {atom['statement'][:110]}")
        return bid
    others = [b for b in store.beliefs() if b["id"] != verdict["target"]]
    targets = [verdict["target"]] + [t for t, _ in contradiction_fanout(atom, day, others, vec, [verdict["target"]],
                                                                         scope_ctx)]
    held = {b["id"]: b for b in store.beliefs()}
    drafts = {}
    for t in targets:
        draft = agents.draft_revision(held[t], atom, day, scope_ctx)
        if draft["outcome"] == "revised":
            drafts[t] = {k: draft[k] for k in ("statement", "kind", "reasoning")}
    bid = store.hold(day, atom, vec, targets, verdict.get("reasoning"), drafts)
    log(f"  {day} [held       ] {bid} contradicts {', '.join(targets)} ({len(drafts)} rewrite(s) drafted): "
        f"{atom['statement'][:110]}")
    return bid


def run_day(day: str, store: IntakeStore, scope_ctx, embed_texts, log=print) -> dict:
    """Extract and judge one day. Marks the day done, or failed and re-raises."""
    try:
        insights, provenance = day_items.insights(day)
        atoms = agents.extract_day(day, insights, provenance, scope_ctx)
        log(f"  {day}: {len(insights)} insights -> {len(atoms)} atoms")
        counts = judge_day(day, atoms, store, scope_ctx, embed_texts, log)
    except Exception as exc:
        store.mark_day(day, "failed", 0, f"{type(exc).__name__}: {exc}")
        raise
    store.mark_day(day, "done", len(atoms))
    return {"day": day, "atoms": len(atoms), **counts}


def pending_days(store: IntakeStore, through: str) -> list[str]:
    """Stored days up to and including `through` that the store has not finished, in date order."""
    done = store.days_done()
    return [d for d in day_items.available_days() if d <= through and d not in done]


def app_store() -> IntakeStore:
    from belief_engine.intake.store import app_db
    return IntakeStore(app_db(), APP_TABLE_PREFIX)


def run_pending(store: IntakeStore, through: str, log=print) -> list[dict]:
    """Every pending day through `through`, in order. Stops at the first failure."""
    from app.assistant.embeddings.embedder import embed_texts
    scope_ctx = agents.scope()
    days = pending_days(store, through)
    log(f"{len(days)} pending day(s) through {through}")
    results = []
    t0 = time.monotonic()
    for day in days:
        results.append(run_day(day, store, scope_ctx, embed_texts, log))
    log(f"{len(results)} day(s) in {time.monotonic() - t0:.0f}s")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--through", required=True, help="last day to process (YYYY-MM-DD)")
    args = parser.parse_args()
    sys.path.insert(0, ".")
    from app.assistant.tests.test_setup import initialize_services
    initialize_services()
    run_pending(app_store(), args.through)
    return 0


if __name__ == "__main__":
    sys.exit(main())
