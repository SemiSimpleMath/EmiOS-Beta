from __future__ import annotations

# All dayflow orchestrator logic has moved to:
#   app.assistant.dayflow_orchestrator.dayflow_tick
#
# This module now only owns the cron routine dispatch registry.
# The re-exports below keep existing importers working without changes.

from app.assistant.dayflow_orchestrator.orchestrator_status import (  # noqa: F401
    block_dayflow_orchestrator_for_master_chat,
    DAYFLOW_ORCHESTRATOR_ROOM_ID,
    DAYFLOW_ORCHESTRATOR_STATUS_RESOURCE_ID,
    MASTER_ROOM_BLOCK_SECONDS,
)


def _lazy_cadence_tick(*, target_date=None, routine=None):
    """Lazy import to avoid circular import at module load time."""
    from app.assistant.dayflow_orchestrator.dayflow_tick import dayflow_orchestrator_cadence_tick
    return dayflow_orchestrator_cadence_tick(target_date=target_date, routine=routine)


dayflow_orchestrator_cadence_tick = _lazy_cadence_tick

def _lazy_situation_audit(*, target_date=None, routine=None):
    """Run the situation auditor — periodic background audit of all active context."""
    from app.assistant.pipelines.dayflow.utils.situation_audit_runner import run_situation_audit
    return run_situation_audit()


situation_audit = _lazy_situation_audit


def _lazy_location_refresh(*, target_date=None, routine=None):
    """Refresh the location manager — rebuild timeline and update resource_current_location.json."""
    from app.assistant.location_manager import get_location_manager
    get_location_manager().refresh()


location_refresh = _lazy_location_refresh


def _lazy_chat_memory_index(*, target_date=None, routine=None):
    """Index recent chat summaries into ChromaDB for conversational memory."""
    from app.assistant.agent_runtime.services.chat_memory_rag import index_recent_summaries
    index_recent_summaries()


chat_memory_index = _lazy_chat_memory_index


def _lazy_proposal_promoter(*, target_date=None, routine=None):
    """Nightly: walk pending claim_proposals and apply promotions."""
    from app.assistant.kg.proposal_promoter import run_promoter
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    commit = bool(spec.get("commit", False))
    limit = int(spec.get("limit", 500))
    return run_promoter(limit=limit, commit=commit)


proposal_promoter = _lazy_proposal_promoter


def _lazy_kg_finding_backlog_drain(*, target_date=None, routine=None):
    """Daily: investigate the oldest N pending kg_maintenance findings (FIFO).

    The weekly kg_maintenance_pipeline only investigates findings produced by
    its own run, leaving older pending findings to accumulate. This drain
    routine picks from the global pending queue so the backlog doesn't grow.
    """
    from app.assistant.kg_investigator.finding_processor import drain_pending_findings
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 5))
    finding_types = spec.get("finding_types") or None
    return drain_pending_findings(limit=limit, finding_types=finding_types)


kg_finding_backlog_drain = _lazy_kg_finding_backlog_drain


def _lazy_wiki_nightly_refresh(*, target_date=None, routine=None):
    """Nightly: incrementally regenerate wiki pages whose KG neighborhood
    changed since the page was last generated. Optionally runs the
    consistency critic on each refreshed page."""
    from pathlib import Path
    from app.assistant.wiki_generator.nightly_refresh import run_nightly_wiki_refresh
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    vault_raw = spec.get("vault_path") or ""
    vault_path = Path(vault_raw) if vault_raw else None
    run_critic = bool(spec.get("run_critic", True))
    return run_nightly_wiki_refresh(vault_path=vault_path, run_critic=run_critic)


wiki_nightly_refresh = _lazy_wiki_nightly_refresh


ROUTINE_FUNCTION_REGISTRY = {
    "dayflow_orchestrator_cadence_tick": dayflow_orchestrator_cadence_tick,
    "situation_audit": situation_audit,
    "location_refresh": location_refresh,
    "chat_memory_index": chat_memory_index,
    "proposal_promoter": proposal_promoter,
    "wiki_nightly_refresh": wiki_nightly_refresh,
}
