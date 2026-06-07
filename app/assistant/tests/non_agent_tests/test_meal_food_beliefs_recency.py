"""Guard: recent user feedback reaches the meal planner regardless of belief weight.

meal_context_builder._build_food_beliefs() ranks established beliefs by
current_net_weight and truncates to a top-N. Fresh feedback has NULL/near-zero
weight, so before the recency lane it was structurally invisible to the planner
(verified live: 5 new feedback beliefs, 0 reached the block — they sank below the
top-30 cutoff and were even cut by the LIMIT-500 weight pre-filter).

This pins the recency lane:
- a belief with a recent user_comment 'confirms' evidence appears even with NULL
  weight and even when many heavier beliefs would fill the established top-N;
- a belief whose only recent comment 'contradicts' it does NOT surface as current
  intent (the stale "kids will eat zucchini" case).

Uses an in-memory SQLite so it never touches the live belief store.
Part of the pre-push regression guard.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.assistant.subconscious.meal_context_builder as mcb

# More than the established-lane cap (RUNAWAY_THRESHOLD=30), so the NULL-weight
# fresh belief is pushed out of the weight-ranked lane and ONLY the recency lane
# can surface it.
_N_FILLERS = 35


def _make_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE user_beliefs (id TEXT PRIMARY KEY, belief_key TEXT, "
            "statement TEXT, current_net_weight REAL, confidence TEXT, status TEXT, "
            "last_confirmed TEXT)"
        ))
        c.execute(text(
            "CREATE TABLE belief_evidence (id TEXT PRIMARY KEY, belief_id TEXT, "
            "source_type TEXT, signal_type TEXT, source_date TEXT, created_at TEXT)"
        ))
    return engine


def _seed(engine, now_iso):
    beliefs = [
        # fresh feedback, NULL weight — must appear via the recency lane
        ("b1", "meal.guardtest.fresh_confirm", "Kids dislike soup", None, "high", "active", now_iso),
        # stale belief refuted by a recent comment — must NOT appear as current intent
        ("b2", "meal.guardtest.contradicted", "Kids will eat zucchini", None, "high", "active", now_iso),
    ]
    # Heavy established food beliefs that fill the weight-ranked top-N.
    for i in range(_N_FILLERS):
        beliefs.append(
            (f"f{i}", f"food.guardtest.filler_{i:02d}", f"Filler belief {i}",
             100.0 + i, "high", "active", now_iso)
        )
    evidence = [
        ("e1", "b1", "user_comment", "confirms", now_iso, now_iso),
        ("e2", "b2", "user_comment", "contradicts", now_iso, now_iso),
    ]
    with engine.begin() as c:
        for r in beliefs:
            c.execute(
                text("INSERT INTO user_beliefs VALUES (:id,:k,:s,:w,:cf,:st,:lc)"),
                {"id": r[0], "k": r[1], "s": r[2], "w": r[3], "cf": r[4], "st": r[5], "lc": r[6]},
            )
        for r in evidence:
            c.execute(
                text("INSERT INTO belief_evidence (id,belief_id,source_type,signal_type,"
                     "source_date,created_at) VALUES (:id,:b,:src,:sig,:sd,:ca)"),
                {"id": r[0], "b": r[1], "src": r[2], "sig": r[3], "sd": r[4], "ca": r[5]},
            )


def test_recent_feedback_reaches_block_even_when_outranked(monkeypatch):
    now_iso = datetime.now(timezone.utc).isoformat()
    engine = _make_db()
    _seed(engine, now_iso)
    Session = sessionmaker(bind=engine)
    # _build_food_beliefs does `from app.models.base import get_session` at call
    # time, so patching the attribute on the module redirects it to our in-mem DB.
    monkeypatch.setattr("app.models.base.get_session", lambda: Session())

    block = mcb._build_food_beliefs()
    recency = block.split("Established food beliefs")[0]

    # Core fix: a NULL-weight fresh 'confirms' belief reaches the planner, via the
    # recency lane, despite 35 heavier beliefs filling the established top-30.
    assert "meal.guardtest.fresh_confirm" in recency, block
    # A belief whose only recent comment refuted it does not pose as current intent,
    # and (being NULL-weight) is outranked out of the established lane too -> absent.
    assert "meal.guardtest.contradicted" not in block, block
    # The weight-ranked lane still works: the heaviest filler (weight 134) is shown.
    assert "food.guardtest.filler_34" in block, block
