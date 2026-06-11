"""Backfill friction_signal onto recent chat_cluster pods.

For each chat_cluster pod minted in the last N days (default 14) that
doesn't already have `metadata.friction_signal`, re-run the pod_classifier
on the pod's body to extract a friction signal, then write it back. Tags,
one_liner, and sections from the re-classification are DISCARDED — we
only want the new field.

Idempotent: pods already carrying `friction_signal` are skipped. Safe to
re-run.

Usage:
    .venv/Scripts/python.exe -m app.assistant.subconscious.friction_backfill
    .venv/Scripts/python.exe -m app.assistant.subconscious.friction_backfill --days 7
    .venv/Scripts/python.exe -m app.assistant.subconscious.friction_backfill --dry-run
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import app.assistant.tests.test_setup  # noqa: F401  bootstrap

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


def classify_friction_only(burst_text: str, tag_vocabulary: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Call pod_classifier on a burst, return ONLY friction_signal (or None)."""
    try:
        agent = DI.agent_factory.create_agent("pod_classifier")
        if agent is None:
            logger.error("pod_classifier agent unavailable")
            return None
        result = agent.action_handler(
            Message(
                agent_input={
                    "burst_content": burst_text,
                    "tag_vocabulary": tag_vocabulary,
                }
            )
        )
        data = result.data if isinstance(getattr(result, "data", None), dict) else {}
        fs = data.get("friction_signal")
        if isinstance(fs, dict) and fs.get("intensity"):
            return fs
        return None
    except Exception as e:
        logger.error("classify_friction_only failed: %s", e)
        logger.debug("classify_friction_only exception", exc_info=True)
        return None


def load_tag_vocabulary() -> Dict[str, Any]:
    import yaml
    path = Path("configs/pod_tags.yaml")
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("tags", {}) if isinstance(data, dict) else {}


def main():
    parser = argparse.ArgumentParser(description="Backfill friction_signal on recent chat_cluster pods.")
    parser.add_argument("--days", type=int, default=14, help="How many days back to scan (default 14).")
    parser.add_argument("--limit", type=int, default=500, help="Max pods to process per run (default 500).")
    parser.add_argument("--dry-run", action="store_true", help="Run classification but do not write back.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify even pods that already have friction_signal in metadata.",
    )
    args = parser.parse_args()

    print(f"Friction backfill — last {args.days} days, limit={args.limit}, dry_run={args.dry_run}, force={args.force}")

    store = PodStore()
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    pods = store.query(kind="chat_cluster", since_utc=since, limit=args.limit)
    print(f"Found {len(pods)} chat_cluster pods in window.")
    if not pods:
        return

    tag_vocab = load_tag_vocabulary()

    stats = {
        "scanned": 0,
        "already_had_friction": 0,
        "newly_added_friction": 0,
        "no_friction_detected": 0,
        "errors": 0,
    }

    for i, pod in enumerate(pods, 1):
        stats["scanned"] += 1
        metadata = dict(pod.metadata) if pod.metadata else {}
        existing = metadata.get("friction_signal")
        if existing and not args.force:
            stats["already_had_friction"] += 1
            print(f"  [{i:>3}/{len(pods)}] skip (already has friction): {pod.pod_id} — {pod.one_liner}")
            continue

        body = (pod.body or "").strip()
        if not body:
            print(f"  [{i:>3}/{len(pods)}] skip (empty body): {pod.pod_id}")
            continue

        print(f"  [{i:>3}/{len(pods)}] classify: {pod.pod_id} — {pod.one_liner[:80]}")
        fs = classify_friction_only(body, tag_vocab)

        if fs is None:
            stats["no_friction_detected"] += 1
            print(f"             → no friction detected")
            continue

        stats["newly_added_friction"] += 1
        print(
            f"             → friction! intensity={fs.get('intensity')} "
            f"kind={fs.get('kind')} subject={fs.get('subject')!r}"
        )
        if fs.get("quote"):
            print(f"               quote: \"{fs['quote'][:120]}\"")

        if args.dry_run:
            continue

        # Persist: update the pod's metadata and write back via PodStore.put
        metadata["friction_signal"] = fs
        metadata["friction_signal_backfilled_at_utc"] = datetime.now(timezone.utc).isoformat()
        pod.metadata = metadata
        try:
            store.put(pod)
        except Exception as e:
            stats["errors"] += 1
            print(f"             ERROR writing back: {e}")

    print()
    print("=" * 60)
    print("Backfill summary:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("=" * 60)


if __name__ == "__main__":
    main()
