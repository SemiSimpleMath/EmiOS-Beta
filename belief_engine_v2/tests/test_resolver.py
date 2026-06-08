"""#2 canonical identity: paraphrases collapse to one belief; identity-relevant
qualifiers (per predicate) split otherwise-equal claims. Deterministic fake embedder
(one-hot per synonym group), no model load."""
from __future__ import annotations

import os
import tempfile

from belief_engine_v2.registry import Registry
from belief_engine_v2.resolver import Resolver
from belief_engine_v2.store import Claim, Store

# Synonym groups → one-hot vectors: same group = cosine 1.0, different = 0.0.
_GROUPS = ["children", "zucchini", "dislike", "onions", "caffeine", "alex", "avoids", "tea", "other"]
_SYN = {
    "kids": "children", "children": "children", "sam and robin": "children",
    "zucchini": "zucchini", "courgette": "zucchini",
    "dislikes": "dislike", "dislike": "dislike",
    "onions": "onions", "caffeine": "caffeine", "alex": "alex",
    "avoids_after": "avoids", "tea": "tea",
}


def _emb(text: str):
    g = _SYN.get(text.strip().lower(), "other")
    v = [0.0] * len(_GROUPS)
    v[_GROUPS.index(g)] = 1.0
    return v


def _verify(phrase: str, candidate_label: str, ctype: str) -> bool:
    """Fake LLM verifier: 'same' iff same synonym group (mirrors the fake embedder)."""
    return _SYN.get(phrase.strip().lower(), "other") == _SYN.get(candidate_label.strip().lower(), "other")


def _resolver(threshold=0.9, verifier=_verify):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    reg = Registry(conn, embedder=_emb, verifier=verifier, threshold=threshold)
    return Resolver(reg), reg


def test_paraphrases_collapse_to_one_identity():
    res, reg = _resolver()
    sig1, _ = res.resolve(Claim(subject="kids", predicate="dislikes", object="zucchini"))
    sig2, _ = res.resolve(Claim(subject="children", predicate="dislike", object="courgette"))
    sig3, _ = res.resolve(Claim(subject="Sam and Robin", predicate="dislike", object="Zucchini"))
    assert sig1 == sig2 == sig3                      # canonical collapse across paraphrase
    assert reg.concept_count("subject") == 1         # one subject concept
    assert reg.concept_count("object") == 1


def test_distinct_objects_get_distinct_identity():
    res, _ = _resolver()
    a, _ = res.resolve(Claim(subject="kids", predicate="dislikes", object="zucchini"))
    b, _ = res.resolve(Claim(subject="kids", predicate="dislikes", object="onions"))
    assert a != b


def test_identity_qualifier_splits_by_predicate():
    res, _ = _resolver()
    # 'avoids_after' declares `time` identity-relevant → different times = different beliefs
    s16, _ = res.resolve(Claim(subject="alex", predicate="avoids_after", object="caffeine",
                               qualifiers={"time": "16:00"}))
    s20, _ = res.resolve(Claim(subject="alex", predicate="avoids_after", object="caffeine",
                               qualifiers={"time": "20:00"}))
    assert s16 != s20
    # 'dislikes' has no identity qualifiers → the time is retrieval-only, same belief
    d1, _ = res.resolve(Claim(subject="alex", predicate="dislikes", object="onions",
                              qualifiers={"time": "16:00"}))
    d2, _ = res.resolve(Claim(subject="alex", predicate="dislikes", object="onions",
                              qualifiers={"time": "20:00"}))
    assert d1 == d2


def test_store_with_resolver_folds_paraphrases_into_one_belief():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    store = Store(path)
    reg = Registry(store.conn, embedder=_emb, verifier=_verify, threshold=0.9)
    store.resolver = Resolver(reg)
    store.append(Claim(subject="kids", predicate="dislikes", object="zucchini",
                       statement_nl="kids dislike zucchini"), source="chat")
    store.append(Claim(subject="children", predicate="dislike", object="courgette",
                       statement_nl="the children won't eat courgette"), source="chat")
    store.append(Claim(subject="Sam and Robin", predicate="dislike", object="Zucchini",
                       statement_nl="Sam and Robin reject zucchini"), source="user_comment")
    assert store.rebuild_projection() == 1           # 3 paraphrases -> ONE canonical belief
    assert store.beliefs()[0]["obs_count"] == 3


def test_no_verifier_never_merges():
    """Fail-safe (principle 5): embedding says 'might be same' (cosine 1.0), but with no
    verifier nothing merges — similarity alone may NEVER collapse two concepts."""
    res, reg = _resolver(verifier=None)
    a, _ = res.resolve(Claim(subject="kids", predicate="dislikes", object="zucchini"))
    b, _ = res.resolve(Claim(subject="children", predicate="dislike", object="courgette"))
    assert a != b                                    # no auto-merge on similarity alone
    assert reg.concept_count("subject") == 2
    assert reg.concept_count("object") == 2


def test_verifier_veto_keeps_distinct():
    """The embedding proposes the merge; the verifier says 'no' → concepts stay distinct."""
    res, reg = _resolver(verifier=lambda *_: False)
    a, _ = res.resolve(Claim(subject="kids", predicate="dislikes", object="zucchini"))
    b, _ = res.resolve(Claim(subject="children", predicate="dislike", object="courgette"))
    assert a != b
    assert reg.concept_count("subject") == 2
