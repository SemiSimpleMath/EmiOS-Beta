"""Authority bands and access-check helpers for pod operations.

Pods reuse the existing scope-authority axis that gates tool execution.
A pod operation's authority requirement is just another `min_authority`
value the scope.authority must clear, same as a tool's `min_authority`
property.

## Bands

  10  public           Any agent. Display-only projections —
                       redacted, format-description, sentinel.
  50  chat surface     Default for content pods (emails, chats,
                       summaries). Most agent surfaces.
  70  gated chat       Sensitive but shareable in conversation —
                       phone area code, DOB year-only, address
                       components without full address.
  99  user-equivalent  Operations the user is expected to verify
                       in-the-loop. master_room with explicit
                       confirm.
 100  courier-only     Non-LLM deterministic code only. Full SSN,
                       full account number, raw artifact bytes.
                       The cap above all LLM-anchored authority.

The 99/100 cap is the key separation: NO LLM agent can read a
100-band projection. Only the courier (the deterministic
substitution code path, e.g., pod_attach inside send_email) ever
runs at 100.

## Per-type defaults, per-pod overrides

Each pod type (identity.ssn, identity.dob, identity.phone, ...)
declares the standard band for each of its projections via its
materializer. A user creating a specific pod can override per-
projection authority at create time — useful for things like
"this phone number is fine to share at 50, not 70" if the user
wants it more openly accessible.

## Denials

A failed authority check raises `PodAuthorityError`. The caller
(usually the pod_fetch tool wrapper) catches and routes to the
existing approval flow — refuse silently, log denial, or escalate
to a user-approval ticket depending on the project's policy. From
this module's perspective, the check is binary: pass or raise.
"""
from __future__ import annotations

from typing import Optional


# Named bands. Use these instead of literal integers in materializers
# so the policy lives in one place.
AUTH_PUBLIC: int = 10
AUTH_CHAT: int = 50
AUTH_GATED: int = 70
AUTH_USER: int = 99
AUTH_COURIER: int = 100


# Default for content pods (emails, chats, summaries) created by the
# existing pod minters. Identity and artifact pods set their own.
DEFAULT_POD_MIN_AUTHORITY: int = AUTH_CHAT


class PodAuthorityError(Exception):
    """Raised when a pod operation's caller doesn't clear the gate.

    Carries the projection name and required band so the approval
    layer can render a meaningful escalation prompt — "agent X
    wants `ssn.full`, which requires authority 100 (courier-only).
    Approve?"
    """
    def __init__(
        self,
        *,
        pod_id: str,
        projection: Optional[str],
        required: int,
        actual: int,
    ):
        self.pod_id = pod_id
        self.projection = projection
        self.required = required
        self.actual = actual
        if projection:
            msg = (
                f"pod {pod_id} projection {projection!r} requires "
                f"authority {required}; caller has {actual}"
            )
        else:
            msg = (
                f"pod {pod_id} requires authority {required}; "
                f"caller has {actual}"
            )
        super().__init__(msg)


class PodScopeError(Exception):
    """Raised when a non-master room tries to read a pod from a different room.

    The room-scoping rule (enforced at pod_search/pod_fetch tool wrappers):
    rooms with authority_level < AUTH_USER (99) can only see pods whose
    `scope_id` matches their own `room_id`. Master_room and system-authority
    contexts bypass this — they can read pods across all rooms.

    Defends against transitive cross-room leakage: a Slack-side planner
    calling pod_search/pod_fetch should not return chat_cluster pods minted
    in master_room or other private contexts.
    """
    def __init__(self, *, pod_id: str, pod_scope: Optional[str], caller_room: Optional[str]):
        self.pod_id = pod_id
        self.pod_scope = pod_scope
        self.caller_room = caller_room
        super().__init__(
            f"pod {pod_id} belongs to scope {pod_scope!r}; "
            f"caller room {caller_room!r} cannot read across scopes"
        )


def caller_authority(scope) -> int:
    """Extract the integer authority level from a scope object.

    Mirrors how tools read scope.approval.authority_level. Defaults to
    0 (no authority) if the scope is None or doesn't carry an approval
    policy — fail-closed.
    """
    if scope is None:
        return 0
    approval = getattr(scope, "approval", None)
    if approval is None:
        return 0
    level = getattr(approval, "authority_level", None)
    if level is None:
        return 0
    try:
        return int(level)
    except (TypeError, ValueError):
        return 0


def check_authority(
    *,
    pod_id: str,
    projection: Optional[str],
    required: int,
    scope,
) -> int:
    """Verify the caller's scope clears `required`. Returns the
    caller's actual authority for audit logging. Raises
    `PodAuthorityError` on failure.
    """
    actual = caller_authority(scope)
    if actual < required:
        raise PodAuthorityError(
            pod_id=pod_id,
            projection=projection,
            required=required,
            actual=actual,
        )
    return actual
