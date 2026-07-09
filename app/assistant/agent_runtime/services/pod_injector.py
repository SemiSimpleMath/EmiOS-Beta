"""PodInjector — scans agent prompt context for pod URIs and hydrates headers.

Mirrors ``EntityInjector`` in role: a stateless component attached to every
agent via ``AgentComponents`` that turns text references (``datapod:kind:id``)
into structured ``PodHeader`` objects the prompt can render.

Agents do not opt in. Any text field the agent sees (incoming_message, task,
information, recent_history) is scanned on every turn.

One-pass termination is structural: ``PodHeader`` contains no pod URIs, so
hydrating headers never produces new references to scan.
"""
from __future__ import annotations

from typing import Any, List, Optional

from app.assistant.pod_store.contracts import PodHeader
from app.assistant.pod_store.pod_uri import hydrate_headers_from_text
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


POD_INJECTION_KEYS: set[str] = {"referenced_pods", "referenced_pods_block"}
DEFAULT_POD_SCAN_KEYS: list[str] = ["incoming_message", "task", "information", "recent_history"]


class PodInjector:
    @staticmethod
    def get_injection_keys() -> set[str]:
        return set(POD_INJECTION_KEYS)

    def hydrate_for_context(
        self,
        *,
        user_context: dict,
        message: Any = None,
        scan_keys: Optional[List[str]] = None,
    ) -> List[PodHeader]:
        """Return PodHeaders for pods referenced in user_context text fields.

        Idempotency: if ``message.referenced_pods`` is already non-empty,
        return it as-is (skip re-scan). Otherwise scan, hydrate, and
        populate ``message.referenced_pods`` as a side effect.
        """
        if message is not None:
            existing = getattr(message, "referenced_pods", None) or []
            if existing:
                return list(existing)

        keys = scan_keys if scan_keys is not None else list(DEFAULT_POD_SCAN_KEYS)
        parts: list[str] = []
        for key in keys:
            value = user_context.get(key) if isinstance(user_context, dict) else None
            if isinstance(value, str) and value.strip():
                parts.append(value)
        if not parts:
            return []

        combined = "\n".join(parts)
        # The caller's scope applies the same authority floor pod_search
        # puts on headers — a header the agent couldn't list via search
        # shouldn't ride into its prompt via a pasted URI either.
        scope = getattr(message, "scope_context", None) if message is not None else None
        headers = hydrate_headers_from_text(combined, scope=scope)

        if message is not None and headers:
            message.referenced_pods = headers

        return headers

    @staticmethod
    def format_block(headers: List[PodHeader]) -> str:
        """Render a prompt-ready block of pod headers with a usage primer."""
        if not headers:
            return ""
        lines = [
            "Referenced pods (IDs are references; call pod_fetch(pod_id) to read the full body):",
        ]
        for h in headers:
            tag_part = f" [{', '.join(h.tags)}]" if h.tags else ""
            ct_part = f" · {h.content_type}" if h.content_type else ""
            lines.append(f"- {h.pod_id}{tag_part}{ct_part}")
            if h.one_liner:
                lines.append(f"    {h.one_liner}")
        return "\n".join(lines)
