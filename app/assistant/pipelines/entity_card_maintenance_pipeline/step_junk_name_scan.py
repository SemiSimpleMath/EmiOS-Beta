"""
Step: junk_name_scan

Finds active entity cards whose entity_name matches known artifact patterns:
  - Pure punctuation or ≤1 alphanumeric character
  - Ammo/ammunition caliber patterns (e.g. ".308 ammo", "9mm ammunition")

These are KG extraction artifacts that slipped through generation.
Suggested action: deactivate.
"""
from __future__ import annotations

import re

from app.assistant.entity_card_maintenance.store import upsert_finding
from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)


def _is_junk(name: str) -> tuple[bool, str]:
    """Returns (is_junk, reason)."""
    if not name or not name.strip():
        return True, "empty name"

    s = name.strip()
    low = s.lower()

    if re.match(r"^\.\d+\b", s) and ("ammo" in low or "ammunition" in low):
        return True, "ammo caliber pattern"
    if ("ammunition" in low or " ammo" in low or low.endswith(" ammo")) and any(ch.isdigit() for ch in low):
        return True, "ammo caliber pattern"

    alnum = sum(1 for ch in s if ch.isalnum())
    if alnum <= 1:
        return True, f"only {alnum} alphanumeric character(s)"

    return False, ""


def run(ctx: PipelineContext) -> dict:
    """Returns {"scanned": int, "new_findings": int}."""
    from app.assistant.entity_management.entity_cards import EntityCard

    session = get_session()
    try:
        rows = (
            session.query(EntityCard.id, EntityCard.entity_name, EntityCard.entity_type)
            .filter(EntityCard.is_active == True)
            .all()
        )
        cards = [(str(r.id), r.entity_name or "", r.entity_type or "") for r in rows]
    finally:
        session.close()

    new_findings = 0
    junk_count = 0
    for card_id, name, etype in cards:
        junk, reason = _is_junk(name)
        if not junk:
            continue

        junk_count += 1
        _, created = upsert_finding(
            finding_type="junk_name",
            entity_card_id=card_id,
            suggested_action="deactivate",
            reason=f"Entity name {name!r} matches junk pattern: {reason}.",
            confidence=0.97,
            priority="high",
            evidence={"entity_name": name, "entity_type": etype, "junk_reason": reason},
            pipeline_run_id=ctx.run_id,
        )
        if created:
            new_findings += 1

    logger.debug(
        "[junk_name_scan] checked=%d junk=%d new_findings=%d",
        len(cards), junk_count, new_findings,
    )
    return {"scanned": junk_count, "new_findings": new_findings}
