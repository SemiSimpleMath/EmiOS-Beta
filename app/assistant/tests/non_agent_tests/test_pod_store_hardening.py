"""Pod store hardening (2026-07-08 audit R1-R8).

Pinned here:
- the URI grammar recognizes dotted kinds (the old regex was blind to
  intention.*/plan.*/auth.* — 14 of 23 live kinds were unreferenceable);
- canonical_pod_id preserves the dot namespace;
- PodStore.put enforces format + registry on NEW pods (post-processing
  enforcement: whatever minted, the store boundary holds the grammar) while
  legacy rows stay updatable;
- the retention sweep applies keep_days / keep_latest and never touches
  pods with projection rows;
- pod_search filters headers by the pod's authority floor;
- mint_pod stamps the originating room (never the scope_id string).
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("USE_TEST_DB", "true")
os.environ.setdefault("TEST_DB_NAME", "test_pod_store_hardening")

import pytest

import app.assistant.tests.test_setup  # noqa: F401

from app.models.base import Base, get_session

from app.assistant.pod_store import pod_utils
from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.models import PodProjection, PodRow
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.pod_store.pod_uri import POD_URI_RE, extract_pod_ids


@pytest.fixture(autouse=True)
def _clean_tables():
    # The suite-shared test DB may carry a pod_store table created by an
    # older module before the importance/min_authority columns existed —
    # create_all(checkfirst) would skip it. Recreate the pod tables at the
    # CURRENT schema and reset PodStore's once-per-process flag so its own
    # ensure path stays a no-op afterwards.
    session = get_session()
    try:
        engine = session.bind
        Base.metadata.drop_all(engine, tables=[PodProjection.__table__, PodRow.__table__])
        Base.metadata.create_all(engine, tables=[PodProjection.__table__, PodRow.__table__])
    finally:
        session.close()
    PodStore._tables_ensured = True
    yield


def _row_count(kind=None):
    session = get_session()
    try:
        q = session.query(PodRow)
        if kind:
            q = q.filter(PodRow.kind == kind)
        return q.count()
    finally:
        session.close()


def _seed_row(pod_id, kind, *, age_days=0, one_liner="x"):
    session = get_session()
    try:
        session.add(PodRow(
            pod_id=pod_id, kind=kind, tags_json=[], one_liner=one_liner,
            body="b", source_refs_json=[], for_agents_json=[],
            created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
        ))
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# URI grammar — dotted kinds are references now
# ---------------------------------------------------------------------------

def test_extract_recognizes_dotted_kinds():
    text = (
        "see datapod:intention.meal:abc123def456 and "
        "datapod:plan.weekly_schedule:0f9e8d7c6b5a plus "
        "datapod:chat_cluster:abc123 and the sealed "
        "datapod:health.private:execcode_148c707348ec4b98."
    )
    assert extract_pod_ids(text) == [
        "datapod:intention.meal:abc123def456",
        "datapod:plan.weekly_schedule:0f9e8d7c6b5a",
        "datapod:chat_cluster:abc123",
        "datapod:health.private:execcode_148c707348ec4b98",
    ]


def test_extract_base_id_of_projection_ref():
    # The /full projection suffix is the courier tools' own convention; the
    # base id still hydrates as a reference.
    assert extract_pod_ids("datapod:auth.bearer:57bd0a55aa/full") == [
        "datapod:auth.bearer:57bd0a55aa",
    ]


def test_canonical_pod_id_preserves_dots():
    pid = pod_utils.canonical_pod_id("intention.meal", "2026-07-08", "dinner")
    assert pid.startswith("datapod:intention.meal:")
    assert POD_URI_RE.fullmatch(pid)


# ---------------------------------------------------------------------------
# put() — the post-processing format gate
# ---------------------------------------------------------------------------

def _pod(pod_id, kind, **kw):
    return Pod(pod_id=pod_id, kind=kind, one_liner="t", body="b", **kw)


def test_put_accepts_registered_dotted_kind():
    pid = pod_utils.canonical_pod_id("intention.meal", "t1")
    PodStore().put(_pod(pid, "intention.meal"))
    assert PodStore().get(pid) is not None


def test_put_rejects_unregistered_kind():
    with pytest.raises(ValueError, match="not registered"):
        PodStore().put(_pod("datapod:zz_unregistered:abc123", "zz_unregistered"))


def test_put_rejects_malformed_id():
    with pytest.raises(ValueError, match="canonical pod URI grammar"):
        PodStore().put(_pod("datapod:note:has-hyphens.txt", "note"))


def test_put_rejects_kind_id_mismatch():
    with pytest.raises(ValueError, match="must be identical"):
        PodStore().put(_pod("datapod:note:abc123", "email"))


def test_put_updates_legacy_row_without_validation():
    # A pre-grammar row (filename id) must keep accepting updates.
    legacy_id = "datapod:tool_result:exec_deadbeef_result.txt"
    _seed_row(legacy_id, "tool_result")
    store = PodStore()
    pod = store.get(legacy_id)
    pod.metadata = {"stored_path": "data/x"}
    store.put(pod)   # update path — grandfathered
    assert store.get(legacy_id).metadata["stored_path"] == "data/x"


# ---------------------------------------------------------------------------
# retention sweep
# ---------------------------------------------------------------------------

def test_retention_sweeps_by_policy(monkeypatch):
    from app.assistant.pod_store import pod_kind_registry as reg
    from app.assistant.pod_store.pod_retention import run_pod_retention_sweep

    policy = {
        "zz_sweep": {"retention": {"mode": "keep_days", "days": 10, "keep_latest": 1}},
        "zz_forever": {"retention": {"mode": "keep_forever"}},
    }
    monkeypatch.setattr(reg, "known_kinds", lambda: sorted(policy))
    monkeypatch.setattr(reg, "get_kind", lambda k: policy.get(k))

    _seed_row("datapod:zz_sweep:aaaaa1", "zz_sweep", age_days=20)
    _seed_row("datapod:zz_sweep:aaaaa2", "zz_sweep", age_days=15)
    _seed_row("datapod:zz_sweep:aaaaa3", "zz_sweep", age_days=1)
    _seed_row("datapod:zz_forever:bbbbb1", "zz_forever", age_days=400)
    # Old but carries a projection row — never swept.
    _seed_row("datapod:zz_sweep:aaaaa4", "zz_sweep", age_days=30)
    session = get_session()
    try:
        session.add(PodProjection(
            id="proj-1", pod_id="datapod:zz_sweep:aaaaa4",
            projection_name="full", min_authority=100, storage_kind="env",
            env_ref="ZZ",
        ))
        session.commit()
    finally:
        session.close()

    summary = run_pod_retention_sweep()
    assert summary["swept"] == {"zz_sweep": 2}
    assert summary["protected_by_projection"] == 1
    remaining = {r for (r,) in get_session().query(PodRow.pod_id).all()}
    assert remaining == {
        "datapod:zz_sweep:aaaaa3",      # inside the window
        "datapod:zz_sweep:aaaaa4",      # projection-protected
        "datapod:zz_forever:bbbbb1",    # keep_forever
    }


def test_retention_malformed_policy_fails_loud(monkeypatch):
    from app.assistant.pod_store import pod_kind_registry as reg
    from app.assistant.pod_store.pod_retention import run_pod_retention_sweep

    monkeypatch.setattr(reg, "known_kinds", lambda: ["zz_bad"])
    monkeypatch.setattr(reg, "get_kind", lambda k: {"retention": {"mode": "keep_days"}})
    with pytest.raises(ValueError, match="retention.days"):
        run_pod_retention_sweep()


# ---------------------------------------------------------------------------
# pod_search authority floor on headers
# ---------------------------------------------------------------------------

def test_pod_search_hides_headers_above_authority(monkeypatch):
    from app.assistant.lib.core_tools.pod_store.pod_store_tool import PodStoreTool
    from app.assistant.utils.pydantic_classes import (
        ScopeApprovalPolicy, ScopeContext, ScopePodPolicy, ToolMessage,
    )

    low = _pod("datapod:note:low111", "note", min_authority=50)
    high = _pod("datapod:note:high11", "note", min_authority=99)
    monkeypatch.setattr(PodStore, "query", lambda self, **kw: [low, high])

    scope = ScopeContext(
        scope_id="s", owner_id="user", actor_id="t", surface="ui",
        approval=ScopeApprovalPolicy(authority_level=50),
        pods=ScopePodPolicy(allowed_scopes=["all"]),
    )
    tm = ToolMessage(
        tool_name="pod_search",
        tool_data={"arguments": {}},
        scope_context=scope,
    )
    result = PodStoreTool().handle_pod_search({}, tm)
    ids = [p["pod_id"] for p in result.data["pods"]]
    assert ids == ["datapod:note:low111"]


# ---------------------------------------------------------------------------
# mint_pod scope rule
# ---------------------------------------------------------------------------

def test_mint_pod_stamps_room_id_only():
    from app.assistant.lib.tools.mint_pod.mint_pod import MintPodTool
    from app.assistant.utils.pydantic_classes import ScopeContext, ToolMessage

    scope = ScopeContext(
        scope_id="scope::ui::master_room::main::abc", owner_id="user",
        actor_id="tester", surface="ui", room_id="master_room",
    )
    tm = ToolMessage(
        tool_name="mint_pod",
        tool_data={"arguments": {"title": "T", "body": "B"}},
        scope_context=scope,
    )
    result = MintPodTool().execute(tm)
    assert result.data["ok"], result.content
    pod = PodStore().get(result.data["pod_id"])
    assert pod.scope_id == "master_room"

    # No room on the scope → owner-only (None), never the scope_id string.
    scope_no_room = ScopeContext(
        scope_id="scope::system::x", owner_id="user", actor_id="tester", surface="internal",
    )
    tm2 = ToolMessage(
        tool_name="mint_pod",
        tool_data={"arguments": {"title": "T2", "body": "B2"}},
        scope_context=scope_no_room,
    )
    result2 = MintPodTool().execute(tm2)
    assert result2.data["ok"], result2.content
    assert PodStore().get(result2.data["pod_id"]).scope_id is None
