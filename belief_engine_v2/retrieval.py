"""Contextual retrieval (#3) — a high-recall RANKER that feeds the LLM, not a gate.

`beliefs_for_context(store, now, ...)` returns a ranked candidate set of ACTIVE beliefs for a
moment, scored by
    w_t·temporal_applicability(applies_when, now) + w_r·recency + w_f·frequency + w_v·relevance + w_u·usage
(scratch/BELIEF-ENGINE-NEW.md §5). `usage` = how often the belief has actually been SURFACED
to a consumer (surfacing_log) — importance earned from use, not just observation.

Design principle (the lesson from merge): deterministic means CANNOT decide relevance — there is
no finite enumeration of why a belief matters today ("the day before a trip", "when it's raining",
"the week he's on leave"). So this layer only does RECALL + RANKING; the powerful routine LLM,
with full day context (calendar, weather, situation), makes the actual relevance call and slots
beliefs into the day. Accordingly:
  • The ONLY hard filter is `status` — deprecated/contradicted/retracted beliefs never surface.
  • `temporal_applicability` is a SOFT score, never a gate: a clear day/time MISS sinks a belief
    in the ranking but NEVER removes it (an LLM that knows "he missed Monday's timesheet" can
    still pull it up). Unknown/unparseable cues stay neutral (fail-open). Nothing is silently
    excluded — silent exclusion was the original bug, and a hard gate is exactly that.
  • `applies_when` rides along on each returned belief so the LLM can reason about the cue itself.
Default `k` is generous on purpose: this is a candidate set for the LLM, not the final answer.

Deterministic + numpy only; the embedder (for the relevance term) is injected and optional.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

_WEEKDAY = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "weds": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}

# Weights are RANKING coefficients, not a normalized mix — `usage` is ADDITIVE (a belief earns a
# boost from being used), so the sum exceeds 1 by design; the score is only ever a sort key. With
# no surfacing data the usage term is 0, so ranking is identical to the pre-usage behavior.
_DEFAULT_WEIGHTS = {"temporal": 0.35, "recency": 0.20, "frequency": 0.15, "relevance": 0.30, "usage": 0.15}
_RECENCY_HALF_LIFE_DAYS = 30.0
_FREQ_SATURATION = 20.0   # obs_count at which the frequency term ≈ 1.0
_USAGE_WINDOW_DAYS = 30.0 # surfacings within this window count toward usage (current use, not all-time)
_USAGE_SATURATION = 10.0  # surfaced-count at which the usage term ≈ 1.0
_AT_WINDOW_MIN = 60       # instant horizon: an `at HH:MM` setpoint peaks within this many minutes
_DEFAULT_K = 40           # generous: a candidate set for the LLM, not the final answer

# Soft temporal scores — a RANKING signal, never a gate. A miss SINKS, it does not exclude.
_T_MATCH = 1.0            # cue matches now → float up
_T_NEUTRAL = 0.5         # evergreen, or a cue we can't evaluate (fail-open, neither boost nor sink)
_T_MISS = 0.2            # cue present and clearly does NOT match now → sink, but stay in contention


def _as_dt(now) -> datetime:
    if isinstance(now, datetime):
        return now
    if isinstance(now, str):
        return datetime.fromisoformat(now)
    raise TypeError(f"now must be datetime or ISO string, got {type(now)!r}")


def _first_business_day(dt: datetime) -> int:
    """Day-of-month of the first Mon–Fri in dt's month (holidays ignored for v0)."""
    d = dt.replace(day=1)
    while d.weekday() >= 5:           # Sat/Sun → advance
        d = d.replace(day=d.day + 1)
    return d.day


def _hhmm(s: str) -> Optional[tuple]:
    try:
        h, m = str(s).strip().split(":")
        return int(h), int(m)
    except Exception:
        return None


def _instant_time_of_day(applies_when: dict, now: datetime) -> float:
    """Soft time-of-day fit for horizon='instant'. `at HH:MM` peaks within _AT_WINDOW_MIN of the
    target; `after`/`before` define an open window. A miss SINKS (does not exclude)."""
    now_min = now.hour * 60 + now.minute
    at = applies_when.get("at")
    if at is not None:
        hm = _hhmm(at)
        if hm:
            at_min = hm[0] * 60 + hm[1]
            return _T_MATCH if at_min <= now_min < at_min + _AT_WINDOW_MIN else _T_MISS
    after, before = applies_when.get("after"), applies_when.get("before")
    if after is not None:
        hm = _hhmm(after)
        if hm and now_min < hm[0] * 60 + hm[1]:
            return _T_MISS
    if before is not None:
        hm = _hhmm(before)
        if hm and now_min >= hm[0] * 60 + hm[1]:
            return _T_MISS
    return _T_MATCH


def temporal_applicability(applies_when: Optional[dict], now: datetime, horizon: str = "day") -> float:
    """SOFT relevance-by-time score in [_T_MISS, _T_MATCH] — a ranking signal, NEVER a gate.

    Two kinds of cue:
      • DAY-SCOPE (`weekday`, `anchor`) — which days the belief is in play. Match → boost, clear
        miss → sink (but still surfaced; the LLM may know it applies anyway).
      • TIME-OF-DAY (`at`, `after`, `before`) — a SLOT hint. In horizon='day' (the routine plans
        the whole day in the morning) it does NOT affect the score — the 21:00 cooling belief
        surfaces at 09:00 carrying its time. In horizon='instant' it shapes the score so a live
        check favors what's firing now.

    Components combine by min() — any clear miss sinks the belief, all-match floats it. None/empty
    cue or an unrecognized one → _T_NEUTRAL (fail-open: never sink a belief we can't evaluate)."""
    if not applies_when:
        return _T_NEUTRAL
    scores = []

    wd = applies_when.get("weekday")
    if wd is not None:
        names = wd if isinstance(wd, (list, tuple)) else [wd]
        targets = {_WEEKDAY.get(str(n).strip().lower()) for n in names}
        targets.discard(None)
        if targets:
            scores.append(_T_MATCH if now.weekday() in targets else _T_MISS)

    anchor = applies_when.get("anchor")
    if anchor is not None:
        a = str(anchor).strip().lower()
        if a in ("first_business_day", "first_business_day_of_month"):
            scores.append(_T_MATCH if now.day == _first_business_day(now) else _T_MISS)
        else:
            scores.append(_T_NEUTRAL)   # unknown anchor (e.g. birthday(robin)) — can't judge

    if any(applies_when.get(k) is not None for k in ("at", "after", "before")):
        scores.append(_instant_time_of_day(applies_when, now) if horizon == "instant" else _T_MATCH)

    if not scores:
        return _T_NEUTRAL               # cue present but no key we understand → fail-open
    return min(scores)


def _recency(last_observed: Optional[str], now: datetime) -> float:
    if not last_observed:
        return 0.5
    try:
        last = datetime.fromisoformat(last_observed)
    except Exception:
        return 0.5
    # Compare tz-naively: real timestamps mix naive (date-only source_date) and
    # aware (full created_at) forms; recency is day-scale, so drop tz (as the
    # store's _age_days does) rather than raise on a naive/aware subtraction.
    age_days = max(0.0, (now.replace(tzinfo=None) - last.replace(tzinfo=None)).total_seconds() / 86400.0)
    return 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


def _frequency(obs_count: Optional[int]) -> float:
    n = float(obs_count or 0)
    return min(1.0, math.log1p(n) / math.log1p(_FREQ_SATURATION))


def _usage(surfaced_count: Optional[int]) -> float:
    """Saturating USE signal: how often the belief was surfaced to a consumer recently. Same
    log-saturation shape as _frequency, so a few surfacings help and it caps (no runaway)."""
    n = float(surfaced_count or 0)
    return min(1.0, math.log1p(n) / math.log1p(_USAGE_SATURATION))


def _unit(vec) -> Optional["np.ndarray"]:
    if np is None or vec is None:
        return None
    v = np.asarray(vec, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def beliefs_for_context(
    store,
    now,
    *,
    entities: Optional[Sequence[str]] = None,
    agent: Optional[str] = None,
    query: Optional[str] = None,
    k: int = _DEFAULT_K,
    horizon: str = "day",
    embedder=None,
    weights: Optional[Dict[str, float]] = None,
    tags: Optional[Sequence[str]] = None,
    include_scores: bool = False,
) -> List[Dict[str, Any]]:
    """A ranked, high-recall candidate set of ACTIVE beliefs for `now` — for the LLM to judge,
    NOT the final relevance verdict. `status` is the only hard filter; temporal/recency/frequency/
    relevance are soft scores, so a wrong-day belief is down-ranked but still returned (the LLM,
    with full context, may know it applies). Each belief carries its `applies_when` for the LLM.

    horizon="day" (default) ranks for whole-day planning (time-of-day is a slot hint, not scored);
    horizon="instant" additionally favors what's firing now. `query`/`entities` drive relevance
    when an `embedder` is given. `agent` is accepted for the API shape (future per-agent scoping).
    `tags` (opt-in) scopes the candidate pool to beliefs carrying ANY of those vocab tags
    (configs/belief_tags.yaml) — how a consumer pulls its slice (e.g. the meal engine). Default
    None keeps the full high-recall set.
    """
    dt = _as_dt(now)
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}

    # Attach domain + apply owner overrides from side tables (both survive
    # projection rebuilds; both optional — degrade gracefully on older stores).
    # belief_category -> domain; belief_user_override -> suppress (filtered out
    # here), statement edit (applied in the loop), and lock (carried for consumers).
    def _has(_n):
        return store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_n,)).fetchone() is not None
    _has_cat, _has_ovr, _has_sid = _has("belief_category"), _has("belief_user_override"), _has("belief_short_id")
    _cols = ["b.*", "c.domain" if _has_cat else "NULL AS domain"]
    _joins = " LEFT JOIN belief_category c ON c.belief_id = b.belief_id" if _has_cat else ""
    if _has_ovr:
        _cols += ["o.statement_override AS _stmt_ovr", "COALESCE(o.locked,0) AS locked"]
        _joins += " LEFT JOIN belief_user_override o ON o.belief_id = b.belief_id"
        _where = "b.status='active' AND COALESCE(o.suppressed,0)=0"
    else:
        _cols += ["NULL AS _stmt_ovr", "0 AS locked"]
        _where = "b.status='active'"
    if _has_sid:
        _cols += ["s.short_id AS _short_id_n"]
        _joins += " LEFT JOIN belief_short_id s ON s.belief_id = b.belief_id"
    else:
        _cols += ["NULL AS _short_id_n"]
    _params: list = []
    if tags and _has("belief_tags"):
        from belief_engine_v2 import tags as _tagmod
        _clean = _tagmod.sanitize(tags)
        # Scope to the tagged slice ONLY when the store has actually been tagged. On an
        # untagged store (no belief_tags rows yet — a fresh install, or before the nightly
        # tagging pass) the tag layer is absent, so stay high-recall (return all) instead of
        # returning nothing.
        if _clean and store.conn.execute("SELECT 1 FROM belief_tags LIMIT 1").fetchone():
            _ph = ",".join("?" for _ in _clean)
            _where += f" AND b.belief_id IN (SELECT belief_id FROM belief_tags WHERE tag IN ({_ph}))"
            _params = _clean
    rows = store.conn.execute(
        f"SELECT {', '.join(_cols)} FROM beliefs b{_joins} WHERE {_where}", _params).fetchall()

    qvec = None
    if embedder is not None and (query or entities):
        text = " ".join([query or ""] + list(entities or [])).strip()
        if text:
            qvec = _unit(embedder(text))

    # USE-frequency: how often each belief was actually SURFACED to a consumer in the recent
    # window (surfacing_log, written by beliefs_for_context's consumers). A belief that keeps
    # getting pulled into context earns importance from USE, not just from being observed.
    # Optional table (older stores lack it) -> {} -> the term is a no-op. Day-resolution window:
    # an ISO-string compare on surfaced_at, which is fine for a 30-day horizon.
    usage_counts: Dict[str, int] = {}
    if _has("surfacing_log"):
        _cut = (dt - timedelta(days=_USAGE_WINDOW_DAYS)).isoformat()
        usage_counts = {
            _r[0]: int(_r[1]) for _r in store.conn.execute(
                "SELECT belief_id, COUNT(*) FROM surfacing_log WHERE surfaced_at >= ? GROUP BY belief_id",
                (_cut,))
        }

    scored: List[Dict[str, Any]] = []
    for r in rows:
        item = dict(r)
        # owner statement edit (belief_user_override) wins over the projected text
        stmt = item.pop("_stmt_ovr", None) or item.get("statement_nl")
        item["statement_nl"] = stmt
        _sid = item.pop("_short_id_n", None)
        item["short_id"] = f"b{int(_sid)}" if _sid is not None else None
        aw = json.loads(item["applies_when"]) if item.get("applies_when") else None
        t = temporal_applicability(aw, dt, horizon)   # SOFT score — never excludes

        rec = _recency(item.get("last_observed"), dt)
        freq = _frequency(item.get("obs_count"))
        use = _usage(usage_counts.get(item.get("belief_id"), 0))
        rel = 0.0
        if qvec is not None and stmt:
            bvec = _unit(embedder(stmt))
            if bvec is not None:
                rel = max(0.0, float(qvec @ bvec))

        score = (w["temporal"] * t + w["recency"] * rec + w["frequency"] * freq
                 + w["relevance"] * rel + w["usage"] * use)
        item["score"] = score
        if include_scores:
            item["_scores"] = {"temporal": t, "recency": rec, "frequency": freq, "relevance": rel, "usage": use}
        scored.append(item)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:k]
