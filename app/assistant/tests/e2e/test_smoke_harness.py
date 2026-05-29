"""Smoke tests for the e2e harness — verify the pipeline assembles cleanly.

These tests EXCLUDE the actual manager.request_handler call to avoid burning
LLM tokens on every CI run. They verify:
  - the sandbox creates a tempfile DB and tears it down
  - the real ROOM.md loader, scope_builder, and manager_factory chain works
  - per_manager rules from ROOM.md land in scope.tools.per_manager
  - real emi.db is untouched

A separate live test (test_live_real_llm.py) covers the actual invocation
end-to-end with real LLMs — gated behind an env var so it's not run by default.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_sandbox_isolates_writes_from_real_db():
    """Writes inside sandboxed_di land in the tempfile, not in real emi.db."""
    from app.assistant.tests.e2e.sandbox_setup import sandboxed_di

    real_db = REPO_ROOT / "emi.db"
    real_existed = real_db.exists()
    # Snapshot whether the agent name we're about to insert already exists
    # in the real DB (it shouldn't — uses a tagged-unique sentinel).
    sentinel = f"e2e_sentinel_{datetime.now(timezone.utc).timestamp()}"

    with sandboxed_di() as sandbox:
        assert sandbox.db_path.exists(), "sandbox DB file should exist during run"

        from app.models.base import get_session
        from app.models.llm_call_log import LLMCallLog
        s = get_session()
        try:
            s.add(LLMCallLog(
                agent_name=sentinel, engine="harness-test", provider="test",
                input_tokens=0, output_tokens=0, cached_tokens=0,
                input_cost_usd=0.0, output_cost_usd=0.0, total_cost_usd=0.0,
                duration_ms=0, status="ok",
            ))
            s.commit()
            count = s.query(LLMCallLog).filter(LLMCallLog.agent_name == sentinel).count()
            assert count == 1, "row should be in sandbox DB"
        finally:
            s.close()

    # After exit: tempfile cleaned up
    assert not sandbox.db_path.exists(), "sandbox DB should be deleted after context exit"

    # Real DB must NOT contain the sentinel row (if real DB exists at all).
    if real_existed:
        con = sqlite3.connect(str(real_db))
        try:
            cur = con.cursor()
            # llm_call_log might not exist in a fresh repo; treat absence as a pass.
            try:
                cur.execute("SELECT count(*) FROM llm_call_log WHERE agent_name=?", (sentinel,))
                count = cur.fetchone()[0]
                assert count == 0, "sentinel must NOT be in real emi.db"
            except sqlite3.OperationalError:
                pass
        finally:
            con.close()


def test_per_manager_rules_load_from_test_room_md():
    """ROOM.md → room_resource_loader → scope_builder → scope.tools.per_manager."""
    from app.assistant.tests.e2e.sandbox_setup import sandboxed_di

    with sandboxed_di():
        from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
        from app.assistant.room_session_manager.services.room_scope_builder import (
            build_scope_contract_for_room_request,
        )
        from app.assistant.tests.e2e.harness import _build_envelope

        room_ctx = load_room_context_for_manager("slack/__test__")
        assert room_ctx, "test slack ROOM.md should load"

        env = _build_envelope(
            surface="slack", room_id="slack/__test__", content="hi",
            speaker_name="TestFriend", speaker_external_id="U_test",
        )
        scope = build_scope_contract_for_room_request(
            room_ctx=room_ctx, envelope=env,
            request_data={"task_allowed_tools": None, "task_except_tools": None},
        )

        # per_manager rules from ROOM.md should be in the built scope
        pm = scope["tools"].get("per_manager", {})
        assert "emi_team_manager" in pm
        assert "web_manager" in pm
        # emi_team narrowed to the safe surface
        emi_allow = pm["emi_team_manager"].get("allow", [])
        assert "web_manager" in emi_allow
        assert "pod_search" in emi_allow
        # Dangerous managers NOT in the allow list
        assert "playwright_manager" not in emi_allow
        assert "bash_manager" not in emi_allow
        assert "personal_admin_manager" not in emi_allow
        # web_manager has http stripped
        assert "http_request" in pm["web_manager"].get("block", [])
        assert "oauth_token_refresh" in pm["web_manager"].get("block", [])

        # Slack authority is 30 (low-trust)
        assert scope["approval"]["authority_level"] == 30


def test_room_manager_instantiates_for_test_slack_room():
    """The full chain from ROOM.md to a live manager instance works."""
    from app.assistant.tests.e2e.sandbox_setup import sandboxed_di

    with sandboxed_di():
        from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
        from app.assistant.room_session_manager.services.room_policy_service import (
            resolve_room_manager_name,
        )
        from app.assistant.ServiceLocator.service_locator import DI

        room_ctx = load_room_context_for_manager("slack/__test__")
        manager_name = resolve_room_manager_name(room_ctx)
        assert manager_name == "room_manager"

        manager = DI.multi_agent_manager_factory.create_manager(manager_name)
        assert manager is not None
        assert getattr(manager, "name", None) == "room_manager"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
