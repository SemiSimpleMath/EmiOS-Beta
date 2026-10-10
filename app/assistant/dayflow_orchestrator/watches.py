"""Standing watches: a main task that runs each time an outside event happens, until a time (the architect's
`repeats_until`; store ops `watch_node`, `end_watch`, and the finalizer's `achieved_keep_open`). Owner, 2026-10-10:
"Success but keep node open if the node is of the type where it needs to be ran repeatedly", for event waits only.

The end is the code's: each planning tick closes the watches whose time has run out (`end_watches`, from the tick's
prep), and the scheduler arms a tick for the soonest end ahead (`watch_end`).
"""
from datetime import datetime
from typing import List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def watch_end(wo) -> Optional[datetime]:
    """When the soonest standing watch of this work object, waiting for its next occurrence, ends; None without one."""
    ends = [end for end in (watch_end_of(wo, n) for n in wo.nodes.values()) if end is not None]
    return min(ends) if ends else None


def end_watches(store, now: datetime) -> List[str]:
    """Close every standing watch whose time has run out while it waits for its next occurrence; returns their refs."""
    ended = []
    for summary in store.list_work_objects():
        if str(summary.get("status") or "").lower() in ("done", "abandoned"):
            continue
        wo = store.load(summary["id"])
        for n in list(wo.nodes.values()):
            end = watch_end_of(wo, n)
            if end is not None and end <= now:
                store.apply("end_watch", {"work_id": wo.id, "node_id": n.id}, actor="watch_end")
                ended.append(f"{wo.id}::{n.id}")
    if ended:
        logger.info("[watches] ended %s", ended)
    return ended


def watch_end_of(wo, node) -> Optional[datetime]:
    """When this task's watch ends, if it is a standing watch waiting for its next occurrence."""
    if not (wo.is_work_unit(node) and node.payload.get("watch") and node.status == "waiting"
            and node.wake_kind == "event"):
        return None
    return datetime.fromisoformat(node.payload["watch"]["until"])
