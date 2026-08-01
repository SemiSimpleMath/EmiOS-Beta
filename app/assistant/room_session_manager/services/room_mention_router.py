"""@mention router — ingress-level intercept for ``@<name> <body>`` text.

Sits next to ``RoomSlashCommandRouter`` and is invoked from
``RoomIngressService.apply_room_mode_context`` BEFORE any agent pipeline
runs. The user's message never reaches the manager; an early reply is sent
and (if the named worker is active) the body is delivered to that worker's
mailbox, where its planner will pick it up at the next cycle boundary.

Recognized syntax:

  ``@<name>``         — addresses the worker. Empty body → friendly nudge.
  ``@<name> <body>``  — addresses the worker, body becomes the steering text.

Resolution:
  - ``<name>`` is matched case-insensitively against the display-name
    registry (``Quimby`` → ``web_manager``, etc.).
  - If unknown name → returns ``None`` (caller treats as plain text and
    lets the agent pipeline see ``@steve please help``).
  - If known but no active invocation of that manager → friendly "no active
    Quimby right now" early reply, no mailbox post.
  - If active → wraps the body with a chat-style header and posts to the
    invocation's mailbox as ``message_type="agent_inject"`` keyed by the
    role ``"planner"``. The manager's drain handler resolves the role to
    its specific planner instance and appends to that agent's runtime-
    injection slot.

Sender owns the wrapping. The header reads:

  +++++ Latest instruction from user (supersedes or amends the task you
        are working on) +++ User: <body> +++++

so that the planner sees obvious framing among its existing prompt content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# Match ``@name`` at the very start of the message. Name is alphanumeric +
# underscore. Capture the name and everything after as separate groups.
_MENTION_RE = re.compile(r"^\s*@([A-Za-z0-9_]+)\b\s*(.*)$", re.DOTALL)


@dataclass
class MentionResult:
    """Mirror of SlashCommandResult shape, so apply_room_mode_context can
    treat it the same way."""
    early_result: Optional[Dict[str, Any]] = None
    continue_pipeline: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


class RoomMentionRouter:
    """Resolve ``@<display_name>`` and deliver to a running worker's mailbox."""

    def __init__(
        self,
        *,
        active_workers_provider,
        worker_resolver,
        base_name_resolver,
        display_names_lister,
        mailbox,
    ) -> None:
        # Injected for testability. In production:
        #   active_workers_provider = list_active_workers
        #   worker_resolver = find_active_invocation_by_display_name (exact match)
        #   base_name_resolver = find_active_invocations_by_base_display_name
        #   display_names_lister = list_active_display_names
        #   mailbox = DI.mailbox
        # All resolver helpers accept an optional ``room_id`` kwarg so we
        # only see invocations the sender's room is allowed to address.
        self._list_active = active_workers_provider
        self._resolve = worker_resolver
        self._resolve_by_base = base_name_resolver
        self._list_display_names = display_names_lister
        self._mailbox = mailbox

    @staticmethod
    def extract_mention(content: str) -> Optional[tuple[str, str]]:
        """Parse leading ``@name`` only when it's the FIRST token.

        Returns ``(name, body)`` on match, else ``None``. ``name`` is the
        raw captured name (case preserved); ``body`` is everything after,
        stripped.
        """
        raw = str(content or "")
        if not raw.strip():
            return None
        match = _MENTION_RE.match(raw)
        if not match:
            return None
        name = (match.group(1) or "").strip()
        body = (match.group(2) or "").strip()
        if not name:
            return None
        return name, body

    def route(
        self,
        *,
        body: str,
        room_id: str,
        meta: Dict[str, Any],
    ) -> Optional[MentionResult]:
        """Dispatch one message. ``None`` means "no @mention, continue normal
        pipeline."

        Resolution order (all scoped to the sender's ``room_id``):
          1. Exact display_name match (``@webby_2`` → ``Webby_2``).
          2. Base-name match: collect everything matching the base name.
             - 0 active → "no active <Base> in this room"
             - 1 active → use it (bare ``@webby`` is unambiguous)
             - 2+ active → ambiguity error listing the explicit names
        """
        parsed = self.extract_mention(body)
        if parsed is None:
            return None
        name, mention_body = parsed

        # 1) Exact match wins (e.g. ``@webby_2``). Scoped to the room.
        worker = self._resolve(name, room_id=room_id)

        # 2) Bare-name path: if no exact match, try to resolve by base
        #    display name. If the requested name isn't in the registry at
        #    all, fall through (so chat_gate can see ``@steve``).
        if worker is None:
            if not self._is_known_display_name(name):
                return None
            base_matches = self._resolve_by_base(name, room_id=room_id) or []
            if len(base_matches) == 0:
                worker = None  # no active worker — handled below
            elif len(base_matches) == 1:
                worker = base_matches[0]
            else:
                # Two or more — refuse to guess. Tell the user the
                # explicit names so they can address one.
                from app.assistant.chat_narrator.display_names import mention_key
                display = self._canonical_display(name)
                sorted_names = sorted(m["display_name"] for m in base_matches)
                names = ", ".join(sorted_names)
                text = (
                    f"Multiple {display}s active: {names}. "
                    f"Address one specifically (e.g. @{mention_key(sorted_names[1])})."
                )
                return MentionResult(
                    early_result=self._build_reply(text=text, meta=meta),
                    continue_pipeline=False,
                    meta=dict(meta),
                )

        if worker is None:
            # Known name (Quimby/Em/...) but nobody addressable is running.
            display = self._canonical_display(name)
            others = [
                n for n in (self._list_display_names(room_id=room_id) or [])
                if n.lower() != display.lower()
            ]
            hint = f" (active now: {', '.join(others)})" if others else ""
            text = f"No active {display} right now — nothing to steer.{hint}"
            return MentionResult(
                early_result=self._build_reply(text=text, meta=meta),
                continue_pipeline=False,
                meta=dict(meta),
            )

        if not mention_body:
            text = (
                f"You addressed {worker['display_name']} but didn't say "
                f"anything after the @ — nothing to forward."
            )
            return MentionResult(
                early_result=self._build_reply(text=text, meta=meta),
                continue_pipeline=False,
                meta=dict(meta),
            )

        wrapped = self._wrap_for_planner(mention_body)
        posted = self._mailbox.post(
            invocation_id=worker["invocation_id"],
            message_type="agent_inject",
            payload={"planner": wrapped},
            from_who="user",
            metadata={"source": "room_mention", "room_id": room_id},
        )

        if posted:
            text = f"→ {worker['display_name']} noted."
            logger.info(
                "[RoomMentionRouter] forwarded @%s to invocation=%s len=%d",
                name, worker["invocation_id"], len(mention_body),
            )
        else:
            text = f"Could not deliver to {worker['display_name']}."
            logger.warning(
                "[RoomMentionRouter] mailbox.post returned False for @%s", name,
            )
        return MentionResult(
            early_result=self._build_reply(
                text=text, meta=meta, sender=worker["display_name"],
            ),
            continue_pipeline=False,
            meta=dict(meta),
        )

    # ------------------------------------------------------------------

    def _is_known_display_name(self, name: str) -> bool:
        """Conservative check: do we have anyone (active or not) with this
        display name in our registry? Implementation-light: if the resolver
        returned None, the name might still be a known display whose worker
        just isn't running. We need a separate check; defer to subclassing
        if needed. For now, treat anything we can't look up as "unknown."

        The active_workers_provider is used to discover this — if the name
        is in the registry but no active invocation exists, the resolver
        returns None. We can't distinguish "unknown name" from "known name
        but inactive" without more info, so we treat both as inactive and
        let the caller's reply text handle it. To avoid hijacking truly
        unrelated @text messages from chat though, callers should use a
        registry-aware resolver.

        Matching accepts the literal display name, its mention key
        (``waffle_graph``), or its mention head (``waffle``) — display names
        with spaces/punctuation are otherwise untypeable in @ syntax.
        """
        from app.assistant.chat_narrator.display_names import manager_for_mention
        return manager_for_mention(name) is not None

    def _canonical_display(self, name: str) -> str:
        from app.assistant.chat_narrator.display_names import (
            display_name_for, manager_for_mention,
        )
        manager = manager_for_mention(name)
        return display_name_for(manager) if manager else name

    @staticmethod
    def _wrap_for_planner(body: str) -> str:
        """Sender-side header so the planner immediately recognizes this is
        a runtime instruction, not part of its baseline plan context."""
        return (
            "+++++ Latest instruction from user (supersedes or amends the "
            "task you are working on) +++ "
            f"User: {body} +++++"
        )

    @staticmethod
    def _build_reply(
        *,
        text: str,
        meta: Dict[str, Any],
        sender: str | None = None,
    ) -> Dict[str, Any]:
        """Mirror the early_result shape used by RoomSlashCommandRouter."""
        out: Dict[str, Any] = {
            "reply_text": text,
            "reply_payload": {"chat": text},
            "outbound_intent": {
                "send": True,
                "reply_text": text,
                "reply_payload": {"chat": text},
            },
        }
        if sender:
            out["sender_display_name"] = sender
        return out
