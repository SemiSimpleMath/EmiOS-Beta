"""Every image pod is described when it is created.

ingest_image_file mints an image pod with the bytes and their metadata. A source that already has a
description passes it in (the camera dispatcher, with its analyzer's output). Every other image —
a chat attachment, an agent's camera snapshot, a file minted by hand — is queued here as it is minted:
`image_pod_captioner` looks at the image with what was said around it and writes one_liner, body,
tags and depicted_entities back to the pod.

What was said: a chat attachment carries `chat_trigger_text` (the message it was sent with) and
`chat_room_id` in its metadata; the captioner reads that message and the room's conversation up to
the image, whole. Other images have no chat.

Until 2026-10-05 this ran only for chat attachments, started by the chat route, and never started:
the thread call lacked a required argument and the failure was logged as a warning. All 25 chat
images since April stayed "[image: png, 58kb, 500x248]". A failed description now raises in its
worker (logged ERROR); the pod stays `pending` and `describe_image_pod` can be run on it again.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

# How far before the image the room's conversation is read.
CHAT_WINDOW = timedelta(minutes=30)

_executor = None
_executor_lock = threading.Lock()


def queue_description(pod_id: str) -> None:
    """Describe the pod on the describer pool (two workers: a bulk mint queues, it does not fan out)."""
    global _executor
    from app.assistant.runtime import MonitoredThreadPoolExecutor
    with _executor_lock:
        if _executor is None:
            _executor = MonitoredThreadPoolExecutor(name="image-describer", owner="image_pod_enrichment",
                                                    max_workers=2)
    _executor.submit(_describe_logged, pod_id)


def _describe_logged(pod_id: str) -> None:
    try:
        describe_image_pod(pod_id)
    except Exception:
        logger.exception("[image_pod_enrichment] describing %s failed; it stays pending", pod_id)
        raise


def describe_image_pod(pod_id: str, *, call=None) -> Dict[str, Any]:
    """Describe one image pod and write the description back. A pod already described is left as it
    is. Raises when the pod or its file is missing, or the captioner returns nothing usable. `call` is
    a test seam for the captioner."""
    store = PodStore()
    pod = store.get(pod_id)
    if pod is None:
        raise LookupError(f"no pod {pod_id}")
    if pod.kind != "image":
        raise ValueError(f"pod {pod_id} is a {pod.kind}, not an image")
    meta = pod.metadata or {}
    if meta.get("vision_extraction_status") == "done":
        return {"status": "already_described", "pod_id": pod_id}

    path = (Path(get_repo_root()) / meta["stored_path"]).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"image file for {pod_id} is missing: {path}")

    room = meta.get("chat_room_id")
    trigger = str(meta.get("chat_trigger_text") or "").strip()
    agent_input = {
        "date_time": _local_now(),
        "image": str(path),
        "image_filename": meta.get("original_filename") or path.name,
        "trigger_message": trigger or "(no accompanying message)",
        "chat_context_block": (_chat_context(room, meta["occurred_at_utc"]) if room
                               else "(the image was not sent in a chat)"),
    }
    data = (call or _caption)(agent_input)
    if not isinstance(data, dict) or not (str(data.get("one_liner") or "").strip()
                                          and str(data.get("body") or "").strip()):
        raise ValueError(f"image_pod_captioner returned no description for {pod_id}: {data!r}")

    pod = store.get(pod_id)       # re-read: write onto the current row
    pod.one_liner = data["one_liner"].strip()
    pod.body = data["body"].strip()
    tags = list(pod.tags or [])
    for t in data.get("tags") or []:
        t = str(t).strip().lower()
        if t and t not in tags:
            tags.append(t)
    pod.tags = tags
    pod.metadata = {**(pod.metadata or {}), "vision_extraction_status": "done",
                    "vision_extraction_at_utc": datetime.now(timezone.utc).isoformat(),
                    "depicted_entities": list(data.get("depicted_entities") or [])}
    store.put(pod)
    logger.info("[image_pod_enrichment] described %s: %s", pod_id, pod.one_liner)
    return {"status": "described", "pod_id": pod_id, "one_liner": pod.one_liner}


def _local_now() -> str:
    from app.assistant.utils.time_utils import get_local_time_str
    return get_local_time_str()


def _caption(agent_input: Dict[str, Any]) -> Any:
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    agent = DI.agent_factory.create_agent("image_pod_captioner")
    scope = load_scope_for_source(
        kind="subsystem", source_id="image_pod_enrichment", actor_id="image_pod_enrichment",
        identity_overrides={"owner_id": "system", "surface": "internal", "scope_id": "pod_store::image_enrichment"},
    )
    result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
    return result if isinstance(result, dict) else getattr(result, "data", None)


def _chat_context(room_id: str, anchor_iso: str) -> str:
    """The room's user and assistant turns in the CHAT_WINDOW up to the image, oldest first, whole."""
    from app.assistant.database.db_handler import UnifiedLog2026
    from app.assistant.utils.time_utils import utc_to_local
    from app.models.base import get_session
    anchor = datetime.fromisoformat(anchor_iso)
    anchor = (anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    session = get_session()
    try:
        rows = (session.query(UnifiedLog2026)
                .filter(UnifiedLog2026.room_id == room_id, UnifiedLog2026.role.in_(("user", "assistant")),
                        UnifiedLog2026.timestamp >= anchor - CHAT_WINDOW,
                        UnifiedLog2026.timestamp <= anchor + timedelta(minutes=1))
                .order_by(UnifiedLog2026.timestamp.asc()).all())
        lines: List[str] = []
        for r in rows:
            ts = r.timestamp if r.timestamp.tzinfo else r.timestamp.replace(tzinfo=timezone.utc)
            who = "assistant" if r.role == "assistant" else (r.speaker_name or "user")
            lines.append(f"- {utc_to_local(ts.isoformat()).strftime('%H:%M')} {who}: {r.message or ''}")
    finally:
        session.close()
    return "\n".join(lines) or "(no chat in the room around the image)"


def image_text(pod_id: str) -> str:
    """An image pod as a reader sees it in place of its id: its description, or that it has none yet."""
    pod = PodStore().get(pod_id)
    if pod is None:
        return f"[image {pod_id}: no longer stored]"
    if (pod.metadata or {}).get("vision_extraction_status") != "done":
        return f"[image {pod_id}: not described yet]"
    return f"[image: {pod.one_liner}. {pod.body}]"


def with_images(text: str, *, describe=None) -> str:
    """The text with each line that is an image pod id (how a chat message carries its attached
    image, ui_inbound_service) replaced by the image's description."""
    describe = describe or image_text
    return "\n".join(describe(line.strip()) if line.strip().startswith("datapod:image:") else line
                     for line in text.split("\n"))
