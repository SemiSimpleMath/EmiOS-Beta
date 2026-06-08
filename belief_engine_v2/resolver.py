"""Canonical identity resolver (#2) — replaces the #1 stub.

`identity_sig = hash(subject_id, predicate_id, object_id, identity-relevant qualifier ids)`,
where the concept ids come from the per-user Registry and the *identity-relevant* qualifiers
are declared PER PREDICATE (so "avoid caffeine after 16:00" and "…after 20:00" are different
beliefs, while a time on a non-time-keyed predicate is retrieval-only). This is the
three-way field split (identity vs retrieval vs display) from the design review.

predicate identity-qualifier policy is a small hand-authored core for v2.0; default = none.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Tuple

from belief_engine_v2.registry import Registry

# Per-predicate identity-relevant qualifier keys. Keyed by normalized predicate phrase
# (v2.0 hand-authored core; later: per predicate-concept, possibly LLM-proposed + reviewed).
IDENTITY_QUALIFIERS: Dict[str, list] = {
    "avoids_after_time": ["time"],
    "avoids_after": ["time"],
    "does_routine": ["weekday", "time"],
    "sets_at_time": ["time"],
    "due_at": ["weekday", "time"],
}


class Resolver:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def resolve(self, claim) -> Tuple[str, Dict[str, Any]]:
        """Return (identity_sig, resolution_detail). Mutates the registry (mint/alias)."""
        sid = self.registry.resolve(claim.subject, "subject")
        pid = self.registry.resolve(claim.predicate, "predicate")
        oid = self.registry.resolve(claim.object, "object")

        qids = []
        pred_norm = (claim.predicate or "").strip().lower()
        for key in IDENTITY_QUALIFIERS.get(pred_norm, []):
            val = (claim.qualifiers or {}).get(key)
            if val is not None and str(val).strip():
                qids.append(f"{key}={str(val).strip().lower()}")

        raw = "|".join([sid or "", pid or "", oid or ""] + sorted(qids))
        identity_sig = "b_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
        return identity_sig, {"subject_id": sid, "predicate_id": pid, "object_id": oid, "qualifier_ids": qids}
