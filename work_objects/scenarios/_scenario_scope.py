"""DEV-ONLY scenario scope for the standalone work_objects harnesses.

Production authority is always DERIVED — dayflow workers run under the
orchestrator room's scope (work_session.room_session_scope), the task runner
under its run scope. Scenarios run the substrate with no room at all, so they
declare a permissive owner-automation scope here. This module lives under
scenarios/ on purpose: nothing in app/ or the substrate imports it.
"""
from __future__ import annotations

import uuid

from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy, ScopeContext, ScopePodPolicy, ScopeToolPolicy, ScopeToolRule)

# Mirrors the work_emi_team_manager narrower-cost roster in
# rooms/dayflow_orchestrator/scope.yaml (dev parity).
_WORK_COORDINATOR_ALLOW = [
    "work_web_manager", "personal_admin_manager", "devices_manager", "playwright_manager",
    "bash_manager", "http_manager", "sandbox_manager", "find_tool", "install_tool", "ask_user",
    "pod_search", "pod_fetch", "mint_pod", "ask_kg", "discover_skills", "read_skill",
]


def scenario_scope(*, work_id: str | None = None, owner_id: str = "user", authority: int = 99,
                   allowed_tools: tuple[str, ...] = ("all",)) -> ScopeContext:
    sid = f"scope::work_objects::{work_id}" if work_id else f"scope::work_objects::{uuid.uuid4().hex}"
    return ScopeContext(
        scope_id=sid,
        room_id=sid,
        owner_id=owner_id,
        actor_id="work_scenario",
        surface="work_objects",
        tools=ScopeToolPolicy(
            allowed_tools=list(allowed_tools),
            per_manager={"work_emi_team_manager": ScopeToolRule(allow=list(_WORK_COORDINATOR_ALLOW))},
        ),
        pods=ScopePodPolicy(allowed_scopes=["all"]),
        approval=ScopeApprovalPolicy(authority_level=authority),
    )
