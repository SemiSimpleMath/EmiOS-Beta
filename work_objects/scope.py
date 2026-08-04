"""
work_objects.scope — the WorkOrchestrator is a scope SOURCE.

It owns the ceiling ScopeContext for a work effort. That scope flows to every
sub-manager it dispatches via the standard scope_adapter, which NARROWS it to
each manager's own scope_contract — so each manager/agent gets EXACTLY the tools
it declares (the work manager gets work_* + the managers it may delegate to; the
web_manager gets its 7 web tools), with no hand-set scope_contract_enforced hacks.

Imports app.* (the ScopeContext model); constructing a scope needs no DI bootstrap.
"""
from __future__ import annotations

import uuid

from app.assistant.utils.pydantic_classes import (
    ScopeApprovalPolicy, ScopeContext, ScopePodPolicy, ScopeToolPolicy, ScopeToolRule)

# The work "room" narrows its coordinator (work_emi_team_manager) to its manager roster — exactly like
# master_room's per_manager.emi_team_manager rule. Mirrors master_room's 16-manager pool with the node
# web worker (work_web_manager) in place of web_manager. Without this, the coordinator's planner prompt
# would render the WHOLE registry (its own hidden_tools/narrower no longer trims — the scope must).
# Keep in sync with work_emi_team_manager's intended delegate roster.
_WORK_COORDINATOR_ALLOW = [
    "work_web_manager", "personal_admin_manager", "devices_manager", "playwright_manager",
    "bash_manager", "http_manager", "sandbox_manager", "find_tool", "install_tool", "ask_user",
    "pod_search", "pod_fetch", "mint_pod", "ask_kg", "discover_skills", "read_skill",
]


def orchestrator_scope(*, work_id: str | None = None, owner_id: str = "user", authority: int = 99,
                       allowed_tools: tuple[str, ...] = ("all",)) -> ScopeContext:
    """The ceiling scope for a work effort — the work 'room'. allowed_tools defaults to the full surface
    ('all'); per_manager narrows the coordinator (work_emi_team_manager) to its delegate roster so its
    planner prompt is the ~16 managers, not the whole registry — exactly like master_room narrows
    emi_team. Each dispatched sub-manager (e.g. work_web_manager) narrows further via its own config.

    ONE STABLE IDENTITY PER EFFORT: with work_id, scope_id AND room_id are both
    `scope::work_objects::{work_id}`, shared by EVERY node of that effort. This is load-bearing for
    pods — a pod is minted with scope_id = the minter's room_id (mint_pod), and a reader's
    allowed_scopes ['self'] expands to its room_id. Without a stable, SHARED room_id each node had a
    different (or null) identity, so every pod a node minted read back as PodNotFound for the next
    node. Sharing it makes an effort's pods mutually visible across its nodes; distinct work_ids stay
    isolated. No work_id -> a random per-call identity (legacy/standalone callers)."""
    sid = f"scope::work_objects::{work_id}" if work_id else f"scope::work_objects::{uuid.uuid4().hex}"
    return ScopeContext(
        scope_id=sid,
        room_id=sid,
        owner_id=owner_id,
        actor_id="work_orchestrator",
        surface="work_objects",
        tools=ScopeToolPolicy(
            allowed_tools=list(allowed_tools),
            per_manager={"work_emi_team_manager": ScopeToolRule(allow=list(_WORK_COORDINATOR_ALLOW))},
        ),
        # Work efforts run UNDER the dayflow orchestrator, whose scope sees pods from
        # every room (dayflow scope.yaml `pods: [all]` — it correlates events across
        # surfaces). The effort scope must carry that same visibility: with the default
        # ["self"] a worker could read only its own effort's pods, so an email/chat pod
        # that its goal referenced was silently invisible (pod_search returned 0 for a
        # pod that existed — the 2026-08-03 forward-Jorma's-email flounder). The effort
        # room_id above still gives MINTED pods one stable per-effort identity;
        # per-pod min_authority bands still gate sensitive content on read.
        pods=ScopePodPolicy(allowed_scopes=["all"]),
        approval=ScopeApprovalPolicy(authority_level=authority),
    )
