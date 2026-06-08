"""#1 event-sourcing guarantees: deterministic fold + reversibility (retract)."""
from __future__ import annotations

import os
import tempfile

from belief_engine_v2.store import Claim, Store


def _store() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # Store creates it fresh
    return Store(path)


def test_affirm_creates_one_belief():
    st = _store()
    st.append(Claim(subject="alex", predicate="dislikes", object="onions",
                    statement_nl="Alex dislikes onions"), source="chat")
    assert st.rebuild_projection() == 1
    b = st.beliefs()[0]
    assert b["statement_nl"] == "Alex dislikes onions"
    assert b["status"] == "active"


def test_same_spo_collapses_even_with_paraphrase():
    # Stub identity already collapses exact (case-normalized) subject/predicate/object.
    st = _store()
    st.append(Claim(subject="kids", predicate="dislike", object="zucchini",
                    statement_nl="kids dislike zucchini"), source="chat")
    st.append(Claim(subject="Kids", predicate="dislike", object="Zucchini",
                    statement_nl="the kids don't like zucchini"), source="user_comment")
    st.rebuild_projection()
    bs = st.beliefs()
    assert len(bs) == 1                       # one belief, two supporting observations
    assert bs[0]["obs_count"] == 2
    assert bs[0]["statement_nl"] == "the kids don't like zucchini"   # latest affirm wins


def test_rebuild_is_deterministic():
    st = _store()
    for i in range(8):
        st.append(Claim(subject=f"s{i % 3}", predicate="p", object=f"o{i % 2}",
                        statement_nl=f"stmt{i}"), source="chat")
    st.rebuild_projection()
    first = [dict(r) for r in st.beliefs()]
    st.rebuild_projection()                   # drop + rebuild the cache
    second = [dict(r) for r in st.beliefs()]
    assert first == second                    # projection is a pure function of the log


def test_retract_removes_the_belief():
    st = _store()
    oid = st.append(Claim(subject="alex", predicate="prefers", object="tea",
                          statement_nl="Alex prefers tea"), source="chat")
    assert st.rebuild_projection() == 1
    st.append(None, source="user_comment", polarity="retract", retract_of=oid)
    assert st.rebuild_projection() == 0       # reversibility: withdrawn support → belief gone


def test_contradiction_lowers_net_keeps_support():
    st = _store()
    st.append(Claim(subject="kids", predicate="eat", object="zucchini",
                    statement_nl="kids will eat zucchini"), source="daily_insight", strength=1.0)
    st.append(Claim(subject="kids", predicate="eat", object="zucchini",
                    statement_nl="kids will eat zucchini"), source="user_comment",
              polarity="contradict", strength=2.0)
    st.rebuild_projection()
    b = st.beliefs()[0]
    assert b["support"] == 1.0 and b["contradiction"] == 2.0
    assert b["net"] == -1.0 and b["status"] == "dormant"
