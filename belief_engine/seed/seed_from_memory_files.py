"""
One-time seeder: import entries from the old flat memory JSON files into the belief DB.

Sources (in order):
  - resources/memory/resource_user_routine.json     → domain: routine
  - resources/memory/resource_user_health.json      → domain: health
  - resources/memory/resource_user_general_prefs.json → domain: general

Mapping:
  entry.key        → belief_key  (prefixed with domain. if not already)
  entry.value      → statement
  entry.confidence → confidence  (high/medium/low)
  entry.scope      → scope       (chronic/temporary)
  entry.status     → status      (active/deprecated; skip deprecated)
  entry.added      → first_observed / last_confirmed
  entry.evidence   → seeded as a single EvidenceInput summary
  entry.tags       → stored but not used structurally

Run:
  python belief_engine/seed/seed_from_memory_files.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger("belief_engine.seed")

# domain tag → belief domain name used in the belief engine
_FILE_DOMAIN_MAP = {
    "resource_user_routine":       "routine",
    "resource_user_health":        "health",
    "resource_user_general_prefs": "general",
    "resource_user_food_prefs":    "food",
}

_MEMORY_FILES = [
    REPO_ROOT / "resources" / "memory" / "resource_user_routine.json",
    REPO_ROOT / "resources" / "memory" / "resource_user_health.json",
    REPO_ROOT / "resources" / "memory" / "resource_user_general_prefs.json",
    REPO_ROOT / "resources" / "memory" / "resource_user_food_prefs.json",
]


def _map_confidence(raw: str | None) -> str:
    v = (raw or "medium").lower()
    return v if v in ("high", "medium", "low") else "medium"


def _map_scope(raw: str | None) -> str:
    v = (raw or "chronic").lower()
    return v if v in ("chronic", "temporary", "daily") else "chronic"


def _make_evidence_summary(entry: dict) -> str | None:
    """Condense evidence list into a single text block for seeding."""
    evidence = entry.get("evidence") or []
    if not evidence:
        return None
    lines = []
    for ev in evidence[:5]:  # cap at 5 items
        text = ev.get("text", "").strip()
        kind = ev.get("kind", "")
        if text:
            lines.append(f"[{kind}] {text}")
    return "\n".join(lines) if lines else None


def seed(dry_run: bool = False) -> None:
    import app.assistant.tests.test_setup  # noqa: F401 — bootstraps DI

    from belief_engine.store.belief_store import BeliefStore, BeliefUpsertRequest, EvidenceInput

    store = BeliefStore()
    stats = {"skipped_duplicate": 0, "skipped_deprecated": 0, "skipped_no_value": 0, "created": 0, "errors": 0}

    for fpath in _MEMORY_FILES:
        if not fpath.exists():
            logger.warning("File not found, skipping: %s", fpath)
            continue

        resource_id = fpath.stem  # e.g. resource_user_routine
        domain = _FILE_DOMAIN_MAP.get(resource_id, "general")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        entries = data.get("entries") or []
        logger.info("Seeding %d entries from %s → domain=%s", len(entries), fpath.name, domain)

        for entry in entries:
            try:
                status = (entry.get("status") or "active").lower()
                if status == "deprecated":
                    stats["skipped_deprecated"] += 1
                    continue

                raw_key = entry.get("key", "").strip()
                value = (entry.get("value") or "").strip()
                if not raw_key or not value:
                    stats["skipped_no_value"] += 1
                    continue

                # Prefix with domain if not already namespaced
                if raw_key.startswith(f"{domain}."):
                    belief_key = raw_key
                else:
                    belief_key = f"{domain}.{raw_key}"

                # Skip if already exists (idempotent re-runs)
                existing = store.get_by_key(belief_key)
                if existing:
                    stats["skipped_duplicate"] += 1
                    logger.debug("Already exists, skipping: %s", belief_key)
                    continue

                added_date = entry.get("added") or datetime.now(timezone.utc).date().isoformat()
                confidence = _map_confidence(entry.get("confidence"))
                scope = _map_scope(entry.get("scope"))

                req = BeliefUpsertRequest(
                    domain=domain,
                    belief_key=belief_key,
                    statement=value,
                    confidence=confidence,
                    scope=scope,
                    status="active",
                    last_confirmed=added_date,
                )

                evidence_inputs = []
                ev_summary = _make_evidence_summary(entry)
                if ev_summary:
                    evidence_inputs.append(EvidenceInput(
                        source_type="manual_seed",
                        source_date=added_date,
                        source_ref=f"memory_file:{resource_id}",
                        signal_type="confirms",
                        summary=f"Seeded from {resource_id} entry '{raw_key}'",
                        raw_text=ev_summary[:500],
                        weight=4.0,
                    ))

                if dry_run:
                    logger.info("[DRY RUN] Would create: %s", belief_key)
                    logger.info("  statement: %s", value[:120])
                else:
                    store.upsert_belief(req, evidence_inputs)
                    logger.info("Created: %s [%s/%s]", belief_key, confidence, scope)

                stats["created"] += 1

            except Exception as exc:
                stats["errors"] += 1
                logger.exception("Error seeding entry key=%s: %s", entry.get("key", "?"), exc)

    print(f"\n=== Seed complete ===")
    print(f"  Created:            {stats['created']}")
    print(f"  Skipped (exists):   {stats['skipped_duplicate']}")
    print(f"  Skipped (no value): {stats['skipped_no_value']}")
    print(f"  Skipped (depr.):    {stats['skipped_deprecated']}")
    print(f"  Errors:             {stats['errors']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed belief DB from old flat memory files.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    args = parser.parse_args()
    seed(dry_run=args.dry_run)
