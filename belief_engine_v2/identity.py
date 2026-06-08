"""Belief identity.

#1 ships a STUB: identity = hash of normalized (subject, predicate, object). This
already collapses exact subject/predicate/object matches, which is enough to prove the
event-sourcing fold. It is deliberately dumb.

#2 REPLACES this with canonical-concept resolution against a per-user registry
(subject_id/predicate_id/object_id + per-predicate identity-relevant qualifiers), so
"kids"/"children"/"Sam and Robin" collapse to one identity. Keep identity_of() as the
single seam the resolver swaps.
"""
from __future__ import annotations

import hashlib
from typing import Any


def _norm(s: Any) -> str:
    return (str(s) if s is not None else "").strip().lower()


def identity_of(claim: "Claim") -> str:  # noqa: F821  (Claim imported lazily by callers)
    """Stub identity: hash(norm(subject)|norm(predicate)|norm(object))."""
    raw = f"{_norm(claim.subject)}|{_norm(claim.predicate)}|{_norm(claim.object)}"
    return "b_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
