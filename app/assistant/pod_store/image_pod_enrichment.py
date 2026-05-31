"""Vision + chat context → enrich an image pod's body/one_liner/tags.

Closes the gap where chat-pasted image pods get minted with empty
content. ingest_image_file gives us the bytes + metadata but writes:

    one_liner: "[image: jpg, 234kb, 1024x768]"
    body: ""
    vision_extraction_status: "pending"

Camera-captured pods get enriched by camera_dispatcher running
scene_analyzer. Chat-pasted pods had nothing analogous until now.
This module provides the analogous pass: take the pod, build chat
context (the trigger message + a few surrounding messages from
unified_log_2026), invoke image_pod_captioner, write back to the pod.

Triggered from process_request.py via a monitored thread so the chat
turn doesn't block on vision LLM latency.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)


def enrich_image_pod(
    *,
    pod_id: str,
    trigger_message: Optional[str] = None,
    chat_room_id: str = "master_room",
    history_minutes: int = 10,
    history_count: int = 6,
) -> Dict[str, Any]:
    """Run vision + chat-context captioning on an image pod.

    Idempotent: if vision_extraction_status is already 'done', no-op.

    Args:
        pod_id: the image pod to enrich.
        trigger_message: the chat-text accompanying the upload. When None,
            we use a best-effort fallback (most recent user message from
            chat_room_id around the pod's occurred_at_utc).
        chat_room_id: which room's history to pull for surrounding context.
        history_minutes: window before the trigger to pull from.
        history_count: max messages to include.

    Returns: summary dict.
    """
    store = PodStore()
    pod = store.get(pod_id)
    if pod is None:
        return {"status": "error", "error": "pod_not_found", "pod_id": pod_id}
    if pod.kind != "image":
        return {"status": "error", "error": "not_image_pod", "kind": pod.kind}

    meta = pod.metadata or {}
    if meta.get("vision_extraction_status") == "done":
        return {"status": "skipped", "reason": "already_done"}

    stored_path = (meta.get("stored_path") or "").strip()
    if not stored_path:
        return {"status": "error", "error": "no_stored_path"}
    abs_path = (Path(get_repo_root()) / stored_path).resolve()
    if not abs_path.is_file():
        return {"status": "error", "error": "file_missing_on_disk", "path": str(abs_path)}

    # Build surrounding chat context. Trigger message is primary; the
    # chat_context_block is supportive.
    if not trigger_message:
        trigger_message = (meta.get("chat_trigger_text") or "").strip()
    chat_context_block = _build_chat_context_block(
        room_id=chat_room_id,
        anchor_utc=meta.get("occurred_at_utc"),
        history_minutes=history_minutes,
        history_count=history_count,
    )

    # Invoke the captioner agent
    try:
        from app.assistant.scope.loader import load_scope_for_source
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import Message
        from app.assistant.utils.time_utils import get_local_time_str

        agent = DI.agent_factory.create_agent("image_pod_captioner")
        if agent is None:
            return {"status": "error", "error": "agent_unavailable"}
        agent_input = {
            "date_time": get_local_time_str(),
            "image": str(abs_path),
            "image_filename": meta.get("original_filename") or abs_path.name,
            "trigger_message": (trigger_message or "").strip() or "(no accompanying message)",
            "chat_context_block": chat_context_block,
        }
        scope = load_scope_for_source(
            kind="subsystem",
            source_id="image_pod_enrichment",
            actor_id="image_pod_enrichment",
            identity_overrides={
                "owner_id": "system",
                "surface": "internal",
                "scope_id": "pod_store::image_enrichment",
            },
        )
        result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
    except Exception as e:
        logger.exception("[image_pod_enrichment] captioner invocation raised")
        return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # Agent return: OneShotAgent returns dict directly; Agent wraps in .data
    data: Optional[Dict[str, Any]] = None
    if isinstance(result, dict):
        data = result
    else:
        candidate = getattr(result, "data", None)
        if isinstance(candidate, dict):
            data = candidate
    if not data:
        logger.warning("[image_pod_enrichment] captioner returned no structured data")
        return {"status": "error", "error": "no_structured_data"}

    one_liner = (data.get("one_liner") or "").strip()
    body = (data.get("body") or "").strip()
    tags = list(data.get("tags") or [])
    depicted = list(data.get("depicted_entities") or [])
    if not one_liner and not body:
        return {"status": "error", "error": "empty_caption"}

    # Write back to the pod
    try:
        # Re-fetch to avoid clobbering anything mutated concurrently
        pod = store.get(pod_id)
        if pod is None:
            return {"status": "error", "error": "pod_disappeared_during_run"}
        if one_liner:
            pod.one_liner = one_liner[:160]
        if body:
            pod.body = body
        # Merge tags — keep any existing, append new (deduped)
        existing_tags = list(pod.tags or [])
        for t in tags:
            t_clean = str(t).strip().lower()
            if t_clean and t_clean not in existing_tags:
                existing_tags.append(t_clean)
        pod.tags = existing_tags

        new_meta = dict(pod.metadata or {})
        new_meta["vision_extraction_status"] = "done"
        new_meta["vision_extraction_at_utc"] = datetime.now(timezone.utc).isoformat()
        new_meta["depicted_entities"] = depicted
        if trigger_message and "chat_trigger_text" not in new_meta:
            new_meta["chat_trigger_text"] = trigger_message
        pod.metadata = new_meta

        store.put(pod)
    except Exception as e:
        logger.exception("[image_pod_enrichment] pod write-back failed")
        return {"status": "error", "error": f"write_back_failed: {e}"}

    return {
        "status": "ok",
        "pod_id": pod_id,
        "one_liner": one_liner[:120],
        "body_chars": len(body),
        "tag_count": len(pod.tags),
        "depicted_entities": depicted,
    }


def _build_chat_context_block(
    *,
    room_id: str,
    anchor_utc: Optional[str],
    history_minutes: int,
    history_count: int,
) -> str:
    """Pull recent chat messages around the pod's occurred_at_utc."""
    try:
        from sqlalchemy import text as sql_text
        from app.models.db_manager import get_db_manager
    except Exception as e:
        logger.warning("[image_pod_enrichment] cannot import db for chat context: %s", e)
        return "(chat context unavailable)"

    # Window: from (anchor - history_minutes) to anchor (inclusive)
    try:
        if anchor_utc:
            anchor_dt = datetime.fromisoformat(anchor_utc.replace("Z", "+00:00"))
        else:
            anchor_dt = datetime.now(timezone.utc)
        if anchor_dt.tzinfo is None:
            anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
    except Exception:
        anchor_dt = datetime.now(timezone.utc)
    cutoff_dt = anchor_dt - timedelta(minutes=history_minutes)
    cutoff_str = cutoff_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    anchor_str = anchor_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")

    try:
        with get_db_manager().read_session() as session:
            rows = session.execute(
                sql_text(
                    """
                    SELECT timestamp, role, speaker_name, message
                    FROM unified_log_2026
                    WHERE room_id = :rid
                      AND source IN ('chat', 'room_slack', 'room_sms', 'room_ui')
                      AND role IN ('user', 'assistant')
                      AND message IS NOT NULL
                      AND TRIM(message) != ''
                      AND timestamp >= :cutoff
                      AND timestamp <= :anchor
                    ORDER BY timestamp DESC
                    LIMIT :lim
                    """
                ),
                {
                    "rid": room_id,
                    "cutoff": cutoff_str,
                    "anchor": anchor_str,
                    "lim": history_count,
                },
            ).fetchall()
    except Exception as e:
        logger.warning("[image_pod_enrichment] chat fetch failed: %s", e)
        return "(chat context fetch failed)"

    if not rows:
        return "(no surrounding chat messages found)"

    lines: List[str] = []
    # rows are most-recent-first; flip to chronological for readability
    for r in reversed(rows):
        ts, role, speaker, msg = r[0], r[1], r[2], r[3]
        ts_short = str(ts)[:19]  # "YYYY-MM-DD HH:MM:SS"
        who = (speaker or "").strip() or (role or "user")
        body = str(msg or "").replace("\n", " ").strip()
        lines.append(f"- [{ts_short}] {who}: {body[:240]}")
    return "\n".join(lines)
