"""Shared pod URI utilities — regex + text → PodHeader hydration.

Used wherever the system scans free-form text for pod references:
the PodInjector, tool argument scanners, audit loggers, etc.

Canonical pod URI shape: ``datapod:<kind>:<id>``
  - ``<kind>`` is snake_case segments joined by dots — the dot is the
    namespace separator (``chat_cluster``, ``intention.meal``,
    ``plan.weekly_schedule``, ``auth.bearer``). This is the ONE kind
    grammar; the ``kind`` column and the id's kind segment use it
    identically.
  - ``<id>`` is a lowercase [a-z0-9_] token, 6+ chars, starting
    alphanumeric (24-hex, uuid hex, 12-hex canonical tokens, and
    ``execcode_<hex>`` all match).

``pod_utils.canonical_pod_id`` builds conforming ids; ``PodStore.put``
enforces the shape on every NEW pod (2026-07-08 audit R1 — the old
regex had no dots, so the injector/linkifier were blind to every
dotted kind).
"""
from __future__ import annotations

import re
from typing import List

from app.assistant.pod_store.contracts import PodHeader
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


POD_URI_RE = re.compile(
    r"\bdatapod:[a-z_][a-z_0-9]*(?:\.[a-z_0-9]+)*:[a-z0-9][a-z0-9_]{5,}\b"
)


def extract_pod_ids(text: str) -> List[str]:
    """Return the list of unique pod URIs found in text, in first-seen order."""
    if not text:
        return []
    seen: set[str] = set()
    ordered: List[str] = []
    for match in POD_URI_RE.finditer(text):
        pid = match.group(0)
        if pid in seen:
            continue
        seen.add(pid)
        ordered.append(pid)
    return ordered


def _pod_to_header(pod) -> PodHeader:
    meta = pod.metadata or {}
    return PodHeader(
        pod_id=pod.pod_id,
        kind=pod.kind,
        tags=list(pod.tags),
        one_liner=pod.one_liner,
        scope_id=pod.scope_id,
        created_by=pod.created_by,
        created_at=pod.created_at,
        content_type=str(meta.get("critic_content_type") or ""),
    )


def hydrate_headers_from_text(text: str, *, store: PodStore | None = None) -> List[PodHeader]:
    """Scan text for pod URIs and return a PodHeader for each live pod.

    Missing pod_ids (agent hallucinations or deleted rows) are logged
    and skipped — they do not raise.
    """
    ids = extract_pod_ids(text)
    if not ids:
        return []
    store = store or PodStore()
    headers: List[PodHeader] = []
    for pid in ids:
        pod = store.get(pid)
        if pod is None:
            logger.warning("hydrate_headers_from_text: pod %s not found in store — skipping.", pid)
            continue
        headers.append(_pod_to_header(pod))
    return headers
