"""Deterministic `/pod expand <prefix>` handler.

Resolves a pod-id prefix against the pods recently surfaced in THIS room (not the global store —
that gives few-character matches and stops anyone fishing for pods they were never shown), reads the
body through the universal scope+authority gate (`pod_utils.read_pod_gated`), and posts it back out
the same transport. No LLM, no agent loop — runs at room ingress and short-circuits the pipeline.
"""
from __future__ import annotations

from typing import Any, List

from app.assistant.pod_store import pod_utils
from app.assistant.pod_store.authority import PodAuthorityError
from app.assistant.pod_store.pod_uri import extract_pod_ids
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_HELP = "Usage: `/pod expand <first few characters of a pod id>` — opens a saved finding shown above."


def _reply(text: str):
    # Lazy import to avoid a module-level cycle with the slash router.
    from app.assistant.room_session_manager.services.room_slash_command_router import SlashCommandResult
    return SlashCommandResult(continue_pipeline=False, early_result={"reply_text": text})


def _token(pod_id: str) -> str:
    """The distinctive `[a-z0-9]{6,}` tail after the last colon."""
    return pod_id.rsplit(":", 1)[-1]


def build_room_scope(room_id: str, surface: str):
    """The room's read scope: authority from ROOM.md policy, pod scopes from the
    shared single-source resolver (scope.yaml when the room declares one, else
    ROOM.md permissions) — the same effective value the manager lane gets, so
    the /pod command and /api/pods can never drift from it."""
    from app.assistant.rooms.room_resource_loader import load_room_context_for_manager
    from app.assistant.room_session_manager.services.room_policy_service import resolve_room_authority_level
    from app.assistant.room_session_manager.services.room_scope_builder import resolve_room_pod_scopes
    from app.assistant.utils.pydantic_classes import ScopeContext, ScopeApprovalPolicy, ScopePodPolicy

    ctx = load_room_context_for_manager(room_id)
    policy = ctx.get("room_policy") or {}
    perms = ctx.get("room_permissions") or {}
    authority = resolve_room_authority_level(room_policy=policy, surface=surface)
    pod_scopes = resolve_room_pod_scopes(room_id=room_id, room_permissions=perms)
    return ScopeContext(
        scope_id=f"pod_command::{room_id}",
        owner_id="user",          # the primary human principal (PRINCIPAL_USER)
        actor_id="pod_command",   # deterministic command actor (no LLM)
        surface=surface or "unknown",
        room_id=room_id,
        approval=ScopeApprovalPolicy(authority_level=int(authority)),
        pods=ScopePodPolicy(allowed_scopes=pod_scopes),
    )


def _recent_pod_ids(room_id: str, surface: str, context_id: str) -> List[str]:
    """Pod ids that appear in the room's recent messages (last 24h), first-seen order, deduped."""
    from app.assistant.room_session_manager.services.room_history_builder import RoomHistoryBuilder
    try:
        msgs = RoomHistoryBuilder().build_messages(
            room_id=room_id, limit=80,
            room_surface=surface or None, room_context_id=context_id or None,
        )
    except Exception as e:
        logger.warning("pod_command: recent-history load failed for room %s: %s", room_id, e)
        return []
    out: List[str] = []
    seen = set()
    for m in msgs or []:
        for pid in extract_pod_ids(getattr(m, "content", "") or ""):
            if pid not in seen:
                seen.add(pid)
                out.append(pid)
    return out


def _one_liner(pod_id: str) -> str:
    """Best-effort header one_liner for disambiguation display (the pod was already surfaced here)."""
    try:
        from app.assistant.pod_store.pod_store import PodStore
        pod = PodStore().get(pod_id)
        return (pod.one_liner or "") if pod else ""
    except Exception:
        return ""


def handle_pod_command(*, cmd_payload: str, room_id: str, surface: str, context_id: str):
    """Entry point for the `/pod` slash command. Currently supports `expand <prefix>`."""
    parts = (cmd_payload or "").strip().split(None, 1)
    sub = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""
    if sub != "expand" or not arg:
        return _reply(_HELP)

    prefix = arg.lower()
    candidates = _recent_pod_ids(room_id, surface, context_id)

    def _is_match(pid: str) -> bool:
        pl = pid.lower()
        return _token(pl).startswith(prefix) or pl.startswith(prefix)

    matches = [pid for pid in candidates if _is_match(pid)]
    if not matches:  # forgiving fallthrough: substring anywhere
        matches = [pid for pid in candidates if prefix in pid.lower()]

    if not matches:
        return _reply(f'No recent pod matches `{arg}`. Use the id shown under "Saved findings".')
    if len(matches) > 1:
        lines = "\n".join(
            f"- `{_token(pid)[:10]}…`{(' — ' + ol) if (ol := _one_liner(pid)) else ''}"
            for pid in matches[:8]
        )
        return _reply(f"That matches several findings — add a few more characters:\n{lines}")

    pod_id = matches[0]
    scope = build_room_scope(room_id, surface)
    try:
        pod = pod_utils.read_pod_gated(pod_id, scope)
    except pod_utils.PodNotFound:
        return _reply(f"No recent pod matches `{arg}`.")
    except PodAuthorityError:
        return _reply("You don't have access to that finding from here.")

    one = pod.get("one_liner") or pod_id
    body = pod.get("body") or "(empty)"
    out = f"*{one}*\n\n{body}"
    srcs = [u for u in (pod.get("source_urls") or []) if isinstance(u, str) and u.strip()]
    if srcs:
        out += "\n\nSources:\n" + "\n".join(f"- {u}" for u in srcs)
    return _reply(out)
