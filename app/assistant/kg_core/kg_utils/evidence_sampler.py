"""Date-stratified evidence sampling (fragility review #6, move 1).

Every prompt consumer slices a node's evidence; "first N" and "last N"
are both wrong shapes for most judgments — a referent-change judge fed
the oldest 30 rows literally cannot see the change, and hub nodes (where
evidence is thickest) get the most truncated view. This sampler covers
the node's WHOLE observation timespan:

  - the earliest and latest rows are always included (era endpoints)
  - the remaining budget is spread across time buckets, denser near the
    present (recent evidence usually matters more)
  - undated rows count as oldest (they sort first, honestly uncertain)

One function; every judge adopts it instead of hand-rolled LIMIT N.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from app.assistant.kg_core.kg_utils.date_compare import as_aware_utc

_MIN_SORT = datetime(1, 1, 1, tzinfo=timezone.utc)


def sample_evidence(
    rows: List[Any],
    *,
    cap: int,
    date_key: Callable[[Any], Optional[Any]],
) -> List[Any]:
    """Pick up to ``cap`` rows covering the whole timespan, denser recent.

    ``date_key`` extracts the row's observation datetime (None allowed).
    Returns rows in chronological order. Fewer rows than cap → all rows.
    """
    if cap <= 0:
        return []
    ordered = sorted(
        rows,
        key=lambda r: (as_aware_utc(date_key(r)) is not None,
                       as_aware_utc(date_key(r)) or _MIN_SORT),
    )
    n = len(ordered)
    if n <= cap:
        return ordered
    if cap == 1:
        return [ordered[-1]]

    # Endpoints always in: the first and latest observations bound the era.
    picked = {0, n - 1}
    budget = cap - 2

    # Two halves of the remaining budget: ~1/3 across the older half of
    # the index space, ~2/3 across the newer half (denser near present).
    older_quota = budget // 3
    newer_quota = budget - older_quota
    half = n // 2

    def _spread(start: int, stop: int, count: int) -> None:
        if count <= 0 or stop <= start:
            return
        span = stop - start
        for i in range(count):
            idx = start + (span * (i + 1)) // (count + 1)
            picked.add(min(max(idx, start), stop - 1))

    _spread(1, half, older_quota)
    _spread(half, n - 1, newer_quota)

    # Spreading can collide on the same index; top up with the newest
    # unpicked rows so the cap is actually used.
    i = n - 2
    while len(picked) < cap and i > 0:
        picked.add(i)
        i -= 1

    return [ordered[i] for i in sorted(picked)]
