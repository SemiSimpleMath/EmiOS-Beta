"""A contradiction reaches every belief it bears on, not only the one dedup named.

Dedup names the ONE held belief a new belief most directly relates to. When that verdict is
`contradicts`, the same new belief usually bears on siblings too — "I am ending intermittent
fasting" contradicts "practices intermittent fasting", "is trying intermittent fasting" and
"delays breakfast for the fast" alike, and revising only the named one left the others asserting
a state that had ended (185-day replay, 2026-09-27: five stale siblings). So dedup is asked again
with the named belief removed, and again for each further `contradicts`, until the verdict is something
else. Every belief found is held with the new one for the owner to decide (run.judge_atom); nothing is
revised here.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger

from belief_engine.intake import agents
from belief_engine.intake.rank import ordered

logger = get_logger(__name__)

# Rounds per contradiction. One contradiction touching more held beliefs than this is a store
# that has not been kept current at all, not a fan-out; the remainder is logged and left.
_MAX_ROUNDS = 5


def contradiction_fanout(atom: dict, day: str, held: list[dict], vector: list, already: list[str],
                         scope_ctx) -> list[tuple[str, dict]]:
    """(belief id, dedup verdict) for every further held belief the atom contradicts.

    `held` are the beliefs as currently held (with sources); `already` the ids the atom has already
    been judged against. Nothing is written.
    """
    visited = set(already)
    hits = []
    for _ in range(_MAX_ROUNDS):
        rest = [b for b in held if b["id"] not in visited]
        if not rest:
            break
        verdict = agents.dedup(atom, day, ordered(rest, vector), scope_ctx)
        if verdict["verdict"] != "contradicts":
            break
        target = verdict["target"]
        visited.add(target)
        hits.append((target, verdict))
    else:
        logger.warning("[intake] %s contradiction fan-out stopped after %d rounds: %s",
                       day, _MAX_ROUNDS, atom["statement"])
    return hits
