"""
Export all active beliefs to a JSON resource file.

Output: resources/kg_derived/resource_user_beliefs.json

Run standalone:
    python -m belief_engine.export.export_beliefs
Or call export_beliefs() directly.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.assistant.utils.path_utils import get_repo_root
from app.assistant.kg_core.user_identity import get_primary_user_name
from belief_engine.store.belief_store import BeliefStore

logger = logging.getLogger(__name__)

_OUTPUT_DIR = get_repo_root() / "resources" / "kg_derived"
_OUTPUT_FILE = "resource_user_beliefs.json"


def export_beliefs(*, domain: Optional[str] = None) -> Path:
    """
    Export active beliefs to JSON.
    If domain is given, export only that domain.
    Returns the path written. Skips write if content is unchanged.
    """
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    store = BeliefStore()

    beliefs = store.list_by_domain(domain) if domain else store.list_all()

    entries = []
    for b in beliefs:
        # Decay v2: prefer the evidence-weighted snapshot band (computed nightly
        # by RecomputeBeliefSnapshotStep). Stored b.confidence is the LLM's
        # extraction-time read; the snapshot is what stays current as evidence
        # accumulates and ages. Fall back to b.confidence if no snapshot yet
        # (fresh belief between Update and Recompute).
        effective_confidence = b.current_confidence_band or b.confidence
        entry: dict = {
            "belief_key": b.belief_key,
            "domain": b.domain,
            "statement": b.statement,
            "confidence": effective_confidence,
            "scope": b.scope,
            "status": b.status,
            "observation_count": b.observation_count,
            "first_observed": b.first_observed,
            "last_confirmed": b.last_confirmed,
        }
        if b.kind:
            entry["kind"] = b.kind
        if b.conditions:
            entry["conditions"] = b.conditions
        entries.append(entry)

    resource = {
        "_metadata": {
            "resource_id": "resource_user_beliefs",
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "domain_filter": domain or "all",
            "entry_count": len(entries),
            "description": (
                f"Living belief set about {get_primary_user_name()} derived by the belief inference engine. "
                "Regenerated after each pipeline run. Do not edit manually."
            ),
        },
        "beliefs": entries,
    }

    out_path = _OUTPUT_DIR / _OUTPUT_FILE

    # Only write if beliefs content actually changed (ignore metadata timestamp).
    new_content = json.dumps({"beliefs": entries}, sort_keys=True, ensure_ascii=False)
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            existing_content = json.dumps({"beliefs": existing.get("beliefs", [])}, sort_keys=True, ensure_ascii=False)
            if existing_content == new_content:
                logger.debug("[export_beliefs] No changes detected — skipping write (%d beliefs)", len(entries))
                return out_path
        except Exception as e:
            # Existing file is unreadable / corrupt — fall through to overwrite,
            # but make the corruption visible.
            logger.warning(
                "[export_beliefs] could not parse existing %s (%s); overwriting",
                out_path, e, exc_info=True,
            )

    # Atomic write: temp file in the same dir, then rename. A crash between
    # write_text and replace leaves the previous good file in place; the
    # replace is atomic on the same filesystem.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(resource, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(out_path)
    logger.info("[export_beliefs] Wrote %d beliefs → %s", len(entries), out_path)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    path = export_beliefs()
    print(f"Exported → {path}")
