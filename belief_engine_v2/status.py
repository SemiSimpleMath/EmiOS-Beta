"""Justification-derived status (#4) — JTMS-lite.

A belief's status and confidence are a PURE FUNCTION of its signed justification set (each
justification: belief_id → observation_id, sign +1/−1, weight). Recompute, never mutate.
Because the set is rebuilt from the log, reversibility is free: a `retract` of a contradicting
observation drops that justification on the next fold and the belief revives — and a previously
outweighed belief flips back to active (scratch/BELIEF-ENGINE-NEW.md §2d, §6).

status (derived):
  active    — support present and not seriously contested; net positive
  contested — meaningful support BOTH ways, too close to confidently assert (held, not asserted)
  dormant   — no surviving support, or contradiction decisively outweighs it (revives if support returns)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

# Minority side ≥ this fraction of the majority → "contested": evidence within ~1.6× both ways,
# so 3:2 is too close to call but 2:1 is decisive (→ active for support, dormant against).
CONTESTED_RATIO = 0.6


@dataclass
class StatusResult:
    status: str
    confidence: float   # |net| / total: 0 = perfectly split, 1 = uncontested
    pos: float
    neg: float
    net: float


def derive_status(justifications: Iterable[Mapping], *, min_support: float = 0.0) -> StatusResult:
    """Fold a belief's justification set into (status, confidence). `sign` > 0 supports,
    `sign` < 0 contradicts; `weight` is the (optionally decayed — #5) magnitude. `min_support` is
    the faded floor: surviving support at or below it counts as no support (a belief decayed to
    near-nothing goes dormant; #4 callers leave it 0.0)."""
    js = list(justifications)
    pos = sum(float(j["weight"]) for j in js if j["sign"] > 0)
    neg = sum(float(j["weight"]) for j in js if j["sign"] < 0)
    net = pos - neg
    total = pos + neg
    confidence = (abs(net) / total) if total > 0 else 0.0

    if pos <= min_support:
        status = "dormant"                                    # never affirmed / withdrawn / faded
    elif neg > 0 and min(pos, neg) >= CONTESTED_RATIO * max(pos, neg):
        status = "contested"                                  # both sides substantial and close
    elif net > 0:
        status = "active"
    else:
        status = "dormant"                                    # contradiction decisively outweighs
    return StatusResult(status, confidence, pos, neg, net)
