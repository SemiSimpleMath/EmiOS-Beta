"""The subconscious daily digest pass — importable core.

Reads the concerns_register (populated by noticer ticks), renders the
markdown digest (new vs ongoing vs recently-resolved concerns + pending
questions), writes it to ``app/subconscious_digests/digest_YYYY-MM-DD.md``,
persists it to unified_log_2026 (so it appears in master_room chat
history) and pushes to live socketio subscribers when available.

Callers:
- routine handler ``digest_run`` (configs/routines/public/subconscious_digest.json)
- CLI ``run_digest.py`` (argparse + prints around this core)
- /subconscious dashboard (read-only helpers)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from app.assistant.subconscious.digest_builder import (
    load_digest_state,
    render_digest,
    save_digest_state,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.time_utils import get_local_time

logger = get_logger(__name__)

_REGISTER_REL = "resources/subconscious/resource_concerns_register.json"
_TICK_LOG_REL = "resources/subconscious/resource_subconscious_tick_log.jsonl"
_DIGEST_DIR_REL = "app/subconscious_digests"


def load_register() -> Dict[str, Any]:
    path = get_repo_root() / _REGISTER_REL
    if not path.is_file():
        return {"active": [], "addressing": [], "resolved": [], "dormant": []}
    return json.loads(path.read_text(encoding="utf-8"))


def load_latest_pending_questions() -> List[Dict[str, Any]]:
    """The noticer puts pending_questions in each tick's output, not in the
    register. Read the last tick's output to surface them in the digest."""
    path = get_repo_root() / _TICK_LOG_REL
    if not path.is_file():
        return []
    try:
        last_line = ""
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    last_line = line
        if not last_line:
            return []
        d = json.loads(last_line)
        return d.get("output", {}).get("pending_questions") or []
    except Exception as e:
        logger.warning("[digest] failed to load pending_questions from tick log: %s", e)
        return []


def _persist_digest_to_unified_log(
    *,
    digest_text: str,
    room_id: str,
    digest_date: str,
) -> bool:
    """Append the digest as an assistant message in unified_log_2026.

    This is what makes the digest appear in the user's chat history when
    master_room loads, regardless of whether socketio relays are currently
    running. Source is `subconscious_digest` so KG / extraction pipelines
    can route it differently than normal chat if needed.
    """
    try:
        from app.assistant.database.db_handler import UnifiedLog2026
        from app.models.base import get_session

        row_id = f"subconscious_digest:{digest_date}:{uuid.uuid4().hex[:12]}"
        now_utc = datetime.now(timezone.utc)
        session = get_session()
        try:
            row = UnifiedLog2026(
                id=row_id,
                timestamp=now_utc,
                role="assistant",
                message=digest_text,
                source="subconscious_digest",
                processed=False,
                room_id=room_id,
                room_surface="socketio",
                direction="outbound",
                speaker_name="Subconscious",
                speaker_role="subconscious",
                content_type="markdown",
                metadata_json={
                    "digest_date": digest_date,
                    "produced_by": "subconscious.digest_runner",
                    "sub_data_type": "subconscious_digest",
                },
            )
            session.add(row)
            session.commit()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    except Exception as e:
        logger.warning("[digest] unified_log write failed: %s", e)
        return False


def run_digest_pass(
    *,
    room_id: str = "master_room",
    force: bool = False,
    post: bool = True,
    write_file: bool = True,
) -> Dict[str, Any]:
    """One full digest pass. Returns a summary dict for routine logs.

    force=True treats every active concern as NEW (ignores the
    previously-surfaced set). post=False skips chat history + live push.
    """
    register = load_register()
    state = load_digest_state()
    pending_questions = load_latest_pending_questions()

    previously_surfaced: Set[str] = (
        set() if force else set(state.get("previously_surfaced_concern_ids") or [])
    )

    digest_text = render_digest(
        register=register,
        previously_surfaced_ids=previously_surfaced,
        pending_questions=pending_questions,
    )

    today_local = get_local_time().strftime("%Y-%m-%d")
    digest_path = None
    if write_file:
        digest_dir = get_repo_root() / _DIGEST_DIR_REL
        digest_dir.mkdir(parents=True, exist_ok=True)
        digest_path = digest_dir / f"digest_{today_local}.md"
        digest_path.write_text(digest_text, encoding="utf-8")

    log_ok = None
    live_ok = None
    if post:
        log_ok = _persist_digest_to_unified_log(
            digest_text=digest_text,
            room_id=room_id,
            digest_date=today_local,
        )
        # Live socketio push for immediate display when subscribers exist.
        from app.assistant.ServiceLocator.service_locator import DI
        try:
            publisher = DI.outbound_chat_publisher
        except AttributeError:
            publisher = None
        if publisher is not None:
            live_ok = bool(publisher.publish(
                sender="Subconscious",
                text=digest_text,
                reply_to={"type": "socketio", "room_id": room_id},
                request_id=f"subconscious_digest_{today_local}",
                sub_data_type=["subconscious_digest"],
            ))

    # Record every currently-active concern as surfaced.
    newly_surfaced_ids: Set[str] = set()
    for bucket in ("active", "addressing"):
        for c in register.get(bucket, []) or []:
            cid = c.get("concern_id")
            if cid:
                newly_surfaced_ids.add(cid)

    state["last_digest_at_utc"] = datetime.now(timezone.utc).isoformat()
    state["previously_surfaced_concern_ids"] = sorted(previously_surfaced | newly_surfaced_ids)
    state["history_count"] = int(state.get("history_count") or 0) + 1
    save_digest_state(state)

    summary = {
        "digest_date": today_local,
        "digest_chars": len(digest_text),
        "active_concerns": len(register.get("active") or []),
        "addressing_concerns": len(register.get("addressing") or []),
        "pending_questions": len(pending_questions),
        "wrote_file": str(digest_path) if digest_path else None,
        "unified_log_ok": log_ok,
        "live_push_ok": live_ok,
    }
    logger.info("[digest] pass complete: %s", summary)
    return summary
