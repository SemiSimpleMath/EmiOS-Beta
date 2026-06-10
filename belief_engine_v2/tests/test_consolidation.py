"""#6 consolidation: duplicate beliefs that formed separately merge via embedding-proposes →
LLM-verifies, justifications union, and the merge is reversible. Verifier-no leaves them apart;
non-candidates are never even checked. Fake embedder + verifier, no model load."""
from __future__ import annotations

import os
import tempfile

from belief_engine_v2.consolidate import consolidate
from belief_engine_v2.store import Claim, Store


def _store() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    return Store(path)


def _grp(s: str) -> str:
    s = s.lower()
    return "veg" if ("zucchini" in s or "courgette" in s) else "other"


def _emb(text: str):
    return [1.0, 0.0] if _grp(text) == "veg" else [0.0, 1.0]


def _verify_same_group(a: str, b: str, ctype: str) -> bool:
    return _grp(a) == _grp(b)


def _seed_veg_dupes(st):
    # same belief, two separate identities (different object) → two beliefs the write path won't merge
    st.append(Claim(subject="kids", predicate="eat", object="zucchini",
                    statement_nl="the kids eat zucchini"), source="daily_insight")
    st.append(Claim(subject="kids", predicate="eat", object="courgette",
                    statement_nl="the children eat courgette"), source="user_comment")
    st.rebuild_projection()


def test_duplicates_merge_and_union_justifications():
    st = _store(); _seed_veg_dupes(st)
    assert len(st.beliefs()) == 2

    res = consolidate(st, embedder=_emb, verifier=_verify_same_group, threshold=0.9)
    assert res["merges"] == 1

    bs = st.beliefs()
    assert len(bs) == 1                                   # the two folded into one survivor
    assert bs[0]["obs_count"] == 2                        # both observations attributed to it
    assert len(st.justifications_for(bs[0]["belief_id"])) == 2   # justifications union


def test_verifier_no_keeps_them_separate():
    st = _store(); _seed_veg_dupes(st)
    res = consolidate(st, embedder=_emb, verifier=lambda *a: False, threshold=0.9)
    assert res["candidates"] == 1 and res["merges"] == 0  # proposed, but the LLM said no
    assert len(st.beliefs()) == 2


def test_non_candidates_are_never_checked():
    st = _store(); _seed_veg_dupes(st)
    st.append(Claim(subject="kids", predicate="eat", object="onions",
                    statement_nl="the kids eat onions"), source="daily_insight")
    st.rebuild_projection()
    assert len(st.beliefs()) == 3

    res = consolidate(st, embedder=_emb, verifier=_verify_same_group, threshold=0.9)
    assert res["candidates"] == 1 and res["merges"] == 1  # only the veg pair was a candidate
    assert len(st.beliefs()) == 2                         # merged veg + standalone onions


def test_structural_recall_catches_framing_divergent_dups():
    # Same subject+predicate, different object, statements that embed FAR apart (orthogonal) — exactly
    # the case sentence-embedding recall misses. pair_same_subject_predicate proposes them anyway; the
    # verifier still decides. (This is what makes the live sweep collapse "standing-break nudges" vs
    # "standing break reminders", which score only ~0.6 cosine.)
    st = _store()
    st.append(Claim(subject="user", predicate="finds_frequent", object="standing nudges",
                    statement_nl="standing-break nudges are too frequent"), source="daily_insight")
    st.append(Claim(subject="user", predicate="finds_frequent", object="stand reminders",
                    statement_nl="reduce the cadence of stand reminders"), source="daily_insight")
    st.rebuild_projection()
    assert len(st.beliefs()) == 2

    emb = lambda t: [1.0, 0.0] if "nudge" in t else [0.0, 1.0]   # the two statements are orthogonal
    yes = lambda a, b, k: True

    off = consolidate(st, embedder=emb, verifier=yes, threshold=0.9)            # default: structural OFF
    assert off["candidates"] == 0 and len(st.beliefs()) == 2                    # embedding misses them

    on = consolidate(st, embedder=emb, verifier=yes, threshold=0.9, pair_same_subject_predicate=True)
    assert on["candidates"] == 1 and on["merges"] == 1                          # proposed via subject+predicate
    assert len(st.beliefs()) == 1                                              # collapsed


def test_merge_is_reversible():
    st = _store(); _seed_veg_dupes(st)
    consolidate(st, embedder=_emb, verifier=_verify_same_group, threshold=0.9)
    assert len(st.beliefs()) == 1

    loser = st.conn.execute("SELECT belief_id FROM belief_merges").fetchone()[0]
    st.unmerge_belief(loser)
    st.rebuild_projection()
    assert len(st.beliefs()) == 2                         # split back out, justifications intact
