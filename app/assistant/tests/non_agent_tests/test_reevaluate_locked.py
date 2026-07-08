"""The reevaluator preserves owner-locked beliefs — no rewrite, no deprecation, no LLM spent.

Runs the real ReevaluateBeliefsStep against a tmp-routed belief DB (belief_db_path honors
TEST_DATABASE_URI_EMI) with Chroma faked out; the agent factory booby-traps create_agent so
the test proves the locked skip happens BEFORE any LLM machinery is touched.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


class _FakeChroma:
    def upsert(self, **kw):
        pass

    def delete(self, belief_id):
        pass

    def count(self):
        return 0

    def search(self, query, *, k=5, domain=None):
        return []


@pytest.fixture()
def routed_db(monkeypatch, tmp_path):
    db = tmp_path / "beliefs.db"
    monkeypatch.setenv("USE_TEST_DB", "true")
    monkeypatch.setenv("TEST_DATABASE_URI_EMI", f"sqlite:///{db.as_posix()}")

    import belief_engine.store.belief_store as bs
    monkeypatch.setattr(bs, "get_belief_chroma", lambda: _FakeChroma())

    # ORM tables FIRST (they carry columns newer than SCHEMA_SQL — kind, valence, snapshots),
    # then ensure_schema adds the non-ORM side tables (tags / short_id / merges / distinct_pairs).
    from app.models.base import Base, get_current_engine
    import belief_engine.db.models  # noqa: F401 — register the belief ORM tables
    Base.metadata.create_all(get_current_engine())
    from belief_engine.db.ensure_schema import ensure_schema
    ensure_schema()
    return db


def test_upsert_rejects_unconfigured_domain(routed_db):
    """Beliefs outside configs/belief_domains.yaml would get no nightly maintenance — the
    store now refuses to create them (the orphaning the `meal` domain was added to end)."""
    from belief_engine.store.belief_store import BeliefStore, BeliefUpsertRequest

    store = BeliefStore()
    with pytest.raises(ValueError, match="unknown belief domain"):
        store.upsert_belief(BeliefUpsertRequest(
            domain="other", belief_key="other.orphan",
            statement="An orphan nothing would ever maintain.",
            confidence="low", scope="chronic",
        ))
    assert store.get_by_key("other.orphan") is None


def test_locked_contested_belief_is_skipped_before_any_llm(routed_db, monkeypatch):
    from belief_engine.store.belief_store import BeliefStore, BeliefUpsertRequest

    store = BeliefStore()
    store.upsert_belief(BeliefUpsertRequest(
        domain="routine", belief_key="routine.corrected.by_owner",
        statement="The owner-corrected statement.", confidence="high", scope="chronic",
    ))
    conn = sqlite3.connect(str(routed_db))
    conn.execute("UPDATE user_beliefs SET locked=1, status='contested' "
                 "WHERE belief_key='routine.corrected.by_owner'")
    conn.commit()
    conn.close()

    class _TrappedFactory:
        def create_agent(self, name):
            raise AssertionError("LLM agent must not be consulted for a locked belief")

    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    monkeypatch.setattr(ServiceLocator, "get",
                        classmethod(lambda cls, name: _TrappedFactory() if name == "agent_factory" else None))

    from belief_engine.pipeline.steps.reevaluate_beliefs import ReevaluateBeliefsStep
    ctx = SimpleNamespace(
        domain="routine", scope_context=None,
        belief_update_result={"contested_keys": ["routine.corrected.by_owner"]},
        reevaluation_result=None,
    )
    result = ReevaluateBeliefsStep().run(ctx)

    assert result["stats"]["locked_skipped"] == 1
    assert result["stats"]["errors"] == 0

    after = BeliefStore().get_by_key("routine.corrected.by_owner")
    assert after.statement == "The owner-corrected statement."
    assert after.status == "contested"   # untouched — /beliefs normalizes on lock going forward
    assert after.locked == 1
