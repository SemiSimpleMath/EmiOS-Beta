"""#8 emergent vocabulary: candidates that recur are PROMOTED to the canonical core; duplicate
candidates are MERGED into an active concept (embedding proposes, LLM verifies). Fake embedder +
verifier, no model load."""
from __future__ import annotations

import sqlite3

from belief_engine_v2.registry import Registry
from belief_engine_v2.vocabulary import review_vocabulary

_GROUPS = ["children", "onions", "zucchini", "other"]
_SYN = {
    "kids": "children", "children": "children", "the kids": "children",
    "onions": "onions", "zucchini": "zucchini", "courgette": "zucchini",
}


def _grp(s: str) -> str:
    return _SYN.get(s.strip().lower(), "other")


def _emb(text: str):
    v = [0.0] * len(_GROUPS)
    v[_GROUPS.index(_grp(text))] = 1.0
    return v


def _verify_same_group(a: str, b: str, ctype: str) -> bool:
    return _grp(a) == _grp(b)


def _reg(verifier=_verify_same_group, threshold=0.9) -> Registry:
    return Registry(sqlite3.connect(":memory:"), embedder=_emb, verifier=verifier, threshold=threshold)


def test_recurring_candidate_is_promoted_singleton_is_not():
    reg = _reg()
    reg.resolve("kids", "subject")
    reg.resolve("children", "subject")          # same concept → 2 aliases (write-time verify yes)
    reg.resolve("onions", "object")             # singleton candidate
    assert reg.concept_count("subject") == 1

    res = review_vocabulary(reg, promote_min_aliases=2)   # no verifier → promotion only
    assert res["promoted"] == 1
    assert len(reg.concepts_by_status("active", "subject")) == 1     # proven → canonical
    assert len(reg.concepts_by_status("candidate", "object")) == 1   # one-off stays candidate


def test_duplicate_candidate_merges_into_active():
    reg = _reg(verifier=lambda *a: False)       # write-time says no → two separate concepts form
    k = reg.resolve("kids", "subject")
    reg.promote(k)                              # k is now part of the canonical core
    c = reg.resolve("children", "subject")      # a duplicate concept (verifier said no at write time)
    assert k != c and reg.concept_count("subject") == 2

    res = review_vocabulary(reg, verifier=lambda a, b, ct: True, promote_min_aliases=99)
    assert res["merged"] == 1
    assert reg.concepts_by_status("candidate", "subject") == []      # duplicate folded away
    assert reg.resolve("children", "subject") == k                   # its phrase now resolves to k


def test_review_verifier_no_keeps_duplicate_separate():
    reg = _reg(verifier=lambda *a: False)
    k = reg.resolve("kids", "subject")
    reg.promote(k)
    reg.resolve("children", "subject")
    res = review_vocabulary(reg, verifier=lambda *a: False, promote_min_aliases=99)
    assert res["merged"] == 0
    assert len(reg.concepts_by_status("candidate", "subject")) == 1  # still separate
