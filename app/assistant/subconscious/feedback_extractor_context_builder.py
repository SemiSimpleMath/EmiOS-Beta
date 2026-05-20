"""Build the injected context dict for the feedback_extractor agent.

Reads the queue of unprocessed feedback.comment pods (via
feedback_service), looks up each comment's target pod for summary
context, and surfaces a sample of recent beliefs filtered to the
subconscious domains for key-reuse hints.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.pod_store import PodStore
from app.assistant.subconscious.feedback_service import (
    list_recent_unprocessed_comments,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)


# Domains the subconscious cares about — used to filter the
# recent_existing_beliefs sample so the prompt doesn't drown in
# unrelated communication / kg beliefs.
_RELEVANT_DOMAINS = {"meal", "wellness", "romantic", "scheduling", "household_routine"}


def build_feedback_extractor_context(*, max_comments: int = 30) -> Dict[str, str]:
    now_local = get_local_time()
    return {
        "date_time": now_local.strftime("%Y-%m-%d %H:%M %Z"),
        "day_of_week": now_local.strftime("%A"),
        "unprocessed_comments_block": _build_unprocessed_comments_block(max_comments),
        "recent_existing_beliefs": _build_recent_existing_beliefs(),
    }


def _build_unprocessed_comments_block(limit: int) -> str:
    comments = list_recent_unprocessed_comments(limit=limit)
    if not comments:
        return "(no unprocessed feedback.comment pods in the queue)"

    store = PodStore()
    lines: List[str] = [f"# {len(comments)} unprocessed comment(s)", ""]
    for c in comments:
        target_pod_id = (c.get("target_pod_id") or "").strip()
        target_summary = ""
        target_kind = ""
        target_date = ""
        target_actors: List[str] = []
        if target_pod_id:
            try:
                target_pod = store.get(target_pod_id)
            except Exception as e:
                logger.warning("[feedback_extractor_ctx] target pod fetch failed for %s: %s", target_pod_id, e)
                target_pod = None
            if target_pod is not None:
                meta = target_pod.metadata or {}
                target_kind = str(target_pod.kind or "")
                target_summary = str(meta.get("summary") or target_pod.one_liner or "")
                target_date = str(meta.get("date") or "")
                target_actors = list(meta.get("actors") or [])

        lines.append(f"## Comment {c.get('pod_id')}")
        lines.append(f"  submitted_at_utc: {c.get('submitted_at_utc')}")
        if c.get("target_scope"):
            lines.append(f"  target_scope: {c.get('target_scope')}")
        lines.append(f"  user said: {c.get('text', '')[:600]}")
        lines.append(f"  target_pod_id: {target_pod_id}")
        if target_kind:
            lines.append(f"  target_kind: {target_kind}")
        if target_date:
            lines.append(f"  target_date: {target_date}")
        if target_actors:
            lines.append(f"  target_actors: {', '.join(target_actors)}")
        if target_summary:
            lines.append(f"  target_summary: {target_summary[:300]}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_recent_existing_beliefs() -> str:
    """Sample of beliefs from the exported beliefs file, filtered to
    subconscious-relevant domains. The exporter writes
    resources/kg_derived/resource_user_beliefs.json after every belief
    pipeline run; we read from there as a snapshot. v0 reads UP TO ~30
    relevant beliefs so the extractor has key-reuse signal without
    drowning."""
    path = get_repo_root() / "resources" / "kg_derived" / "resource_user_beliefs.json"
    if not path.is_file():
        return "(no exported beliefs file yet — extractor may invent new keys freely)"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("[feedback_extractor_ctx] beliefs read failed: %s", e)
        return "(error reading beliefs)"

    beliefs = data.get("beliefs") or []
    if not isinstance(beliefs, list) or not beliefs:
        return "(beliefs file present but empty)"

    relevant = [b for b in beliefs if isinstance(b, dict) and (b.get("domain") or "") in _RELEVANT_DOMAINS]
    if not relevant:
        return "(no beliefs in subconscious-relevant domains yet — invent keys freely, follow the schema)"

    # Sort by status=active first, then by observation_count desc
    relevant.sort(
        key=lambda b: (
            0 if (b.get("status") or "") == "active" else 1,
            -int(b.get("observation_count") or 0),
        ),
    )
    relevant = relevant[:30]

    lines: List[str] = [f"# {len(relevant)} existing belief(s) in subconscious domains:", ""]
    for b in relevant:
        key = b.get("belief_key", "?")
        domain = b.get("domain", "?")
        conf = b.get("confidence", "?")
        scope = b.get("scope", "?")
        obs = b.get("observation_count", 0)
        statement = (b.get("statement") or "").replace("\n", " ").strip()
        lines.append(f"- `{key}` [{domain}/{conf}/{scope}, obs={obs}]")
        lines.append(f"  {statement[:240]}")
    return "\n".join(lines)
