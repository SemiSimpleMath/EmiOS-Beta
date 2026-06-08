"""#4 justification-derived status: status + confidence are a pure function of the signed
justification set; the set is first-class (queryable); and retracting a contradiction revives
the belief. No model load."""
from __future__ import annotations

import os
import tempfile

from belief_engine_v2.status import derive_status
from belief_engine_v2.store import Claim, Store


def _store() -> Store:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd); os.remove(path)
    return Store(path)


def _J(sign, weight):
    return {"sign": sign, "weight": weight}


def _affirm(st, strength=1.0, source="daily_insight"):
    return st.append(Claim(subject="kids", predicate="eat", object="zucchini",
                           statement_nl="kids will eat zucchini"), source=source, strength=strength)


def _contradict(st, strength=1.0, source="user_comment"):
    return st.append(Claim(subject="kids", predicate="eat", object="zucchini",
                           statement_nl="kids will eat zucchini"), source=source,
                     polarity="contradict", strength=strength)


# ── derive_status unit behavior ──────────────────────────────────────────────
def test_derive_status_bands():
    assert derive_status([_J(1, 1), _J(1, 2)]).status == "active"        # support only
    assert derive_status([_J(1, 2), _J(-1, 1)]).status == "active"       # 2:1 for → decisive
    assert derive_status([_J(1, 1), _J(-1, 2)]).status == "dormant"      # 2:1 against → decisive
    assert derive_status([_J(1, 3), _J(-1, 2)]).status == "contested"    # 3:2 → too close
    assert derive_status([_J(-1, 1)]).status == "dormant"                # only contradiction
    assert derive_status([]).status == "dormant"
    tie = derive_status([_J(1, 3), _J(-1, 3)])
    assert tie.status == "contested" and tie.confidence == 0.0           # perfectly split
    conf = derive_status([_J(1, 4), _J(-1, 1)])                          # 4:1 for
    assert conf.status == "active" and abs(conf.confidence - 0.6) < 1e-9  # |3|/5


# ── status derived through the store/fold ────────────────────────────────────
def test_active_dormant_contested_through_fold():
    st = _store(); _affirm(st, 1.0); st.rebuild_projection()
    assert st.beliefs()[0]["status"] == "active"

    st = _store(); _affirm(st, 1.0); _contradict(st, 2.0); st.rebuild_projection()
    assert st.beliefs()[0]["status"] == "dormant"                        # contradiction outweighs

    st = _store(); _affirm(st, 3.0); _contradict(st, 2.0); st.rebuild_projection()
    b = st.beliefs()[0]
    assert b["status"] == "contested" and abs(b["confidence"] - 0.2) < 1e-9   # |1|/5


def test_justification_set_is_first_class():
    st = _store()
    _affirm(st, 1.0); a2 = _affirm(st, 1.0); _contradict(st, 1.0)
    st.rebuild_projection()
    bid = st.beliefs()[0]["belief_id"]
    js = st.justifications_for(bid)
    assert len(js) == 3                                                  # one row per surviving obs
    assert sorted(j["sign"] for j in js) == [-1, 1, 1]

    st.append(None, source="user_comment", polarity="retract", retract_of=a2)
    st.rebuild_projection()
    js2 = st.justifications_for(bid)
    assert len(js2) == 2                                                 # retracted obs drops its row


def test_retract_of_contradiction_revives_belief():
    """Reversibility (§6): a belief outweighed by a contradiction flips back to active when that
    contradiction is retracted — status is recomputed, never a one-way delete."""
    st = _store()
    _affirm(st, 1.0)
    cid = _contradict(st, 2.0)
    st.rebuild_projection()
    assert st.beliefs()[0]["status"] == "dormant"

    st.append(None, source="user_comment", polarity="retract", retract_of=cid)
    st.rebuild_projection()
    b = st.beliefs()[0]
    assert b["status"] == "active"                                       # revived
    assert b["contradiction"] == 0.0 and b["net"] == 1.0
