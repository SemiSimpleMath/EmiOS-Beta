"""
Export all active beliefs to a JSON resource file.

Output: resources/kg_derived/resource_user_beliefs.json

Run standalone:
    python -m belief_engine.export.export_beliefs
Or call export_beliefs() directly.
"""
from __future__ import annotations

from app.assistant.utils.logging_config import get_logger
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.assistant.utils.path_utils import get_resources_dir
from app.assistant.kg_core.user_identity import get_primary_user_name

logger = get_logger(__name__)

# get_resources_dir(), NOT get_repo_root()/"resources": the CONSUMER reads
# via get_resources_dir(), which honours EMI_DATA_DIR. get_repo_root() does
# not, so under Docker this wrote the export into the IMAGE
# (/app/resources/kg_derived) while every reader looked on the volume
# (/data/resources/kg_derived) -- they could never meet. Observed as a
# permanent "Beliefs file not found at /data/resources/kg_derived/
# resource_user_beliefs.json" on every dayflow routine run, so routines
# silently ran with no belief context. get_resources_dir() falls back to
# get_repo_root()/"resources" when EMI_DATA_DIR is unset, so dev is
# unchanged.
_OUTPUT_DIR = get_resources_dir() / "kg_derived"
_OUTPUT_FILE = "resource_user_beliefs.json"


def export_beliefs() -> Path:
    """
    Export the active belief catalog to JSON.

    Since the 2026-09-29 cutover the catalog is the intake store (belief_intake_beliefs, ids B<n>),
    read through belief_engine.intake.catalog, which gives each entry the shape consumers already
    read. Returns the path written. Skips write if content is unchanged.
    """
    from belief_engine.intake.catalog import active_entries

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    entries = active_entries()

    resource = {
        "_metadata": {
            "resource_id": "resource_user_beliefs",
            "schema_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "entry_count": len(entries),
            "description": (
                f"Living belief set about {get_primary_user_name()} from the belief intake. "
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
