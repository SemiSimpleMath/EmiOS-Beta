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
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


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
    exclude_finding_types = spec.get("exclude_finding_types") or None
    return drain_pending_findings(
        limit=limit,
        finding_types=finding_types,
        exclude_finding_types=exclude_finding_types,
    )


kg_finding_backlog_drain = _lazy_kg_finding_backlog_drain


def _lazy_kg_date_gap_drain(*, target_date=None, routine=None):
    """Daily: route captured date-gap ANSWERS into the finding executor,
    then gate + ask up to N new curated questions (kg_maintenance::
    date_gap_gate decides worth and crafts the question; swivel-chair-grade
    ownership gets the finding rejected). Core lives in
    kg_maintenance/date_gap_drain.py.

    Routine spec:
      limit          — max questions to ask this run (default 3)
      cooldown_days  — re-ask interval (default 7)
    """
    from app.assistant.kg_maintenance.date_gap_drain import run_date_gap_drain

    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    return run_date_gap_drain(
        limit=max(1, int(spec.get("limit", 3))),
        cooldown_days=max(0, int(spec.get("cooldown_days", 7))),
    )


kg_date_gap_drain = _lazy_kg_date_gap_drain


def _lazy_entity_card_refresh(*, target_date=None, routine=None):
    """Nightly: keep entity cards live (audit 2026-06-10 — the v2 design's
    refresh loop was never wired; cards sat frozen for weeks). Stale cards
    rebuild on a per-card cooldown + nightly cap so cards never churn; newly
    card-worthy entities get cards (critic-vetoed if content is noise);
    orphaned cards deactivate.

    Routine spec: cooldown_days (default 7), max_rebuilds (10), max_new (5).
    """
    from app.assistant.pipelines.entity_cards_v2.refresh_subscriber import run_card_refresh
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    return run_card_refresh(
        cooldown_days=int(spec.get("cooldown_days", 7)),
        max_rebuilds=int(spec.get("max_rebuilds", 10)),
        max_new=int(spec.get("max_new", 5)),
    )


entity_card_refresh = _lazy_entity_card_refresh


def _lazy_kg_identity_sentence_refresh(*, target_date=None, routine=None):
    """Nightly: (re)generate identity sentences — the graph-derived definite
    descriptions that uniquely identify each Entity's referent (the identity
    layer; see scratch/IDENTITY_SENTENCE_SPEC.md). NULL-sentence nodes
    backfill first (pagerank order), then nodes whose discriminating inputs
    (label / era dates / anchor edges) drifted per identity_inputs_hash.
    Writes preserve updated_at (a projection is not a content change).

    Routine spec: max_per_run (default 50).
    """
    from app.assistant.kg_core.kg_utils.identity_sentence import run_identity_sentence_refresh
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    return run_identity_sentence_refresh(
        max_per_run=max(1, int(spec.get("max_per_run", 50))),
    )


kg_identity_sentence_refresh = _lazy_kg_identity_sentence_refresh


def _lazy_kg_evidence_digestion(*, target_date=None, routine=None):
    """Nightly: fold the oldest evidence rows of the heaviest-evidenced
    nodes into one consolidated digest row each (chat-compaction move for
    kg_node_evidence; see scratch/EVIDENCE_HUB_BUDGETS_SPEC.md). Originals
    are stamped digested_at and kept — nothing is deleted.

    Routine spec: max_nodes_per_run (default 5).
    """
    from app.assistant.kg_core.kg_utils.evidence_digestion import run_evidence_digestion
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    return run_evidence_digestion(
        max_nodes_per_run=max(1, int(spec.get("max_nodes_per_run", 5))),
    )


kg_evidence_digestion = _lazy_kg_evidence_digestion


def _lazy_kg_embedding_diff_sync(*, target_date=None, routine=None):
    """Hourly: reconcile chroma label+identity+context collections against
    sqlite (ghosts removed, missing embedded, text drift re-embedded). Free
    local MiniLM; no-op when in sync. Fragility review #4 — drift on the
    resolution path cannot survive an hour.

    Bounded by max_embeds/run (spec, default 1200) so a large backlog (e.g.
    the boot self-heal dropping a collection) converges over several runs
    instead of breaching the watchdog."""
    from app.assistant.kg.chroma.embedding_sync import sync_kg_embeddings
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    return sync_kg_embeddings(
        mode="diff", max_embeds=max(1, int(spec.get("max_embeds", 1200))),
    )


kg_embedding_diff_sync = _lazy_kg_embedding_diff_sync


def _lazy_kg_embedding_full_rebuild(*, target_date=None, routine=None):
    """Nightly: re-embed every node's label + identity sentence — the
    floor; drift cannot survive 24h by construction."""
    from app.assistant.kg.chroma.embedding_sync import sync_kg_embeddings
    return sync_kg_embeddings(mode="full")


kg_embedding_full_rebuild = _lazy_kg_embedding_full_rebuild


def _lazy_kg_state_decay(*, target_date=None, routine=None):
    """Nightly: auto-close State/Event nodes whose TTL has elapsed.

    Runs the same `step_state_decay.run` from the weekly maintenance
    pipeline, but on its own daily schedule. Cheap (no LLM), deterministic
    (uses TTL from `attributes.ttl` set at promotion time + per-node
    `last_observed`), and high-value: keeps the graph from accumulating
    ghost-open states between weekly maintenance runs.
    """
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_state_decay import run as run_state_decay
    ctx = PipelineContext.for_date(pipeline_id="kg_state_decay")
    return run_state_decay(ctx)


kg_state_decay = _lazy_kg_state_decay


def _lazy_kg_goal_dormancy_sweep(*, target_date=None, routine=None):
    """Nightly: flip long-silent Goal nodes from `active` to `dormant`.

    Distinct from state_decay (which auto-CLOSES State/Event nodes by
    setting end_date) — goal_dormancy_sweep only mutates
    attributes.goal_status. ANY re-observation in the promoter flips
    dormant → active. Achievement/abandonment closures are a separate
    terminal path handled by goal_outcome_detector.

    Threshold per Goal: max(ttl.estimated_duration_days * 1.5, 30) days,
    capped at 730. Default 90d when no TTL.
    """
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_goal_dormancy import run as run_goal_dormancy
    ctx = PipelineContext.for_date(pipeline_id="kg_goal_dormancy_sweep")
    return run_goal_dormancy(ctx)


kg_goal_dormancy_sweep = _lazy_kg_goal_dormancy_sweep


def _lazy_kg_goal_outcome_detect(*, target_date=None, routine=None):
    """Daily: scan recent chat for explicit achievement / abandonment of
    active Goal nodes. Closes the matching Goals as terminal (sets
    end_date, attributes.goal_status='achieved'|'abandoned'). Conservative
    — only acts on direct user statements; silent abandonment is
    dormancy_sweep's job, not this one's.
    """
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 10))
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_goal_outcome_detect import run as run_outcome
    ctx = PipelineContext.for_date(pipeline_id="kg_goal_outcome_detect")
    return run_outcome(ctx, limit=limit)


kg_goal_outcome_detect = _lazy_kg_goal_outcome_detect


def _lazy_kg_finding_cluster_resolve(*, target_date=None, routine=None):
    """Daily: read pending kg_maintenance_findings, group by primary_node_id,
    and call kg_finding_cluster_resolver to verify whether each candidate
    cluster shares ONE root question.

    Confirmed clusters get a lead (with synthesized root_question stored on
    its evidence_json.cluster) plus siblings stamped superseded_by=lead_id.
    The maintenance UI hides supersededs by default — distilling N redundant
    findings into 1 surfaced question. No auto-resolution; the lead still
    goes through the normal review/execute path, but resolving it cascades
    the verdict to its siblings.
    """
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_cluster_resolve import run as run_cluster_resolve
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    # Per-run cap: each cluster is >= 1 LLM call; bound them so a large pending
    # backlog converges over runs (superseded findings drop out of candidates)
    # instead of one graph-wide grind. Paired with max_run_seconds in config.
    max_clusters = int(spec.get("max_clusters_per_run", 30))
    ctx = PipelineContext.for_date(pipeline_id="kg_finding_cluster_resolve")
    return run_cluster_resolve(ctx, max_clusters=max_clusters)


kg_finding_cluster_resolve = _lazy_kg_finding_cluster_resolve


def _lazy_kg_finding_executor_drain(*, target_date=None, routine=None):
    """Daily: pick the oldest INVESTIGATED findings whose 24h grace
    window has expired and let kg_resolution_manager apply the
    investigator's prose recommendation. Closes the self-healing loop:
       pending → investigated  (by kg_finding_backlog_drain)
       investigated + auto_apply + 24h grace expired → executed/dismissed/escalated (by THIS routine)
       investigated + needs_user_review → waits for user to respond on /kg-maintenance

    Findings on the auto_apply track go through a 24h grace window
    where a dev can edit / accept / decline via the dev page; if no
    intervention happens, this routine auto-applies the recommendation.
    The Accept button on the dev page calls execute_one() directly to
    bypass the grace window for immediate run.

    Limit defaults to 10/day to keep LLM cost bounded; raise if backlog grows.
    """
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 10))
    from app.assistant.kg_investigator.finding_executor import run_executable_findings
    return run_executable_findings(limit=limit)


kg_finding_executor_drain = _lazy_kg_finding_executor_drain


def _lazy_kg_dup_cluster_drain(*, target_date=None, routine=None):
    """Cluster-resolve the pending duplicate_node backlog.

    Union-finds the pending duplicate_node findings into connected components
    and adjudicates each (merge true variants / keep recurring occurrences
    distinct) in ONE LLM call per cluster — vs the per-pair investigator that
    cannot drain ~1400 findings serially. Dry-run by default; writes a full
    JSON report to scratch/dup_cluster_report.json for review.
    """
    import json

    from app.assistant.kg_investigator.duplicate_cluster_drain import (
        drain_duplicate_clusters,
    )
    from app.assistant.utils.path_utils import get_repo_root

    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    raw_limit = spec.get("limit")
    limit = int(raw_limit) if raw_limit else None
    dry_run = bool(spec.get("dry_run", True))

    result = drain_duplicate_clusters(limit=limit, dry_run=dry_run)

    # Persist the full report (carries node labels -> PII; scratch is gitignored).
    try:
        out = get_repo_root() / "scratch" / "dup_cluster_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    except Exception:
        logger.warning("[kg_dup_cluster_drain] could not write report file", exc_info=True)

    # Return a compact summary; keep the PII-bearing per-cluster reports out of
    # the routine decision log.
    return {k: v for k, v in result.items() if k != "reports"}


kg_dup_cluster_drain = _lazy_kg_dup_cluster_drain


def _lazy_kg_state_date_drain(*, target_date=None, routine=None):
    """Daily: drain pending state_missing_dates findings through the
    investigator. Each finding gets the specialized state_missing_dates
    brief (dated neighbors, conversation-evidence window, sibling states),
    and the investigator proposes fill_dates with confidence. Confident
    proposals (>= 0.8) get applied automatically by the next
    kg_finding_executor_drain run; lower-confidence ones surface as
    user-facing questions.
    """
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("limit", 8))
    from app.assistant.kg_investigator.finding_processor import run_pending_findings
    return run_pending_findings(limit=limit, finding_types=["state_missing_dates"])


kg_state_date_drain = _lazy_kg_state_date_drain


def _lazy_kg_wiki_inference(*, target_date=None, routine=None):
    """Daily: read recently-updated wiki pages and propose new edges/nodes
    that the page implies but the KG doesn't yet have. Each proposal flows
    through the standard claim_proposal pipeline; the promoter applies the
    same merge gates as for chat-extracted facts.

    Bounded by `subject_limit` (default 10) to keep LLM cost predictable.
    Designed to run after wiki_nightly_refresh so the freshest pages get
    inspected.
    """
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    limit = int(spec.get("subject_limit", 10))
    from app.assistant.pipelines.context import PipelineContext
    from app.assistant.pipelines.kg_maintenance_pipeline.step_wiki_inference import run as run_wiki_inference
    ctx = PipelineContext.for_date(pipeline_id="kg_wiki_inference")
    return run_wiki_inference(ctx, subject_limit=limit)


kg_wiki_inference = _lazy_kg_wiki_inference


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


def _lazy_kg_importance_rater(*, target_date=None, routine=None):
    """Periodic: rate edges via LLM, then DERIVE all node importance from edges.

    Architecture (2026-05-12): one LLM rater (edges only). Node importance
    is mechanical derivation:
      - Entity / Concept: `max(adjacent edge.importance)`
      - State / Event / Goal / Property: `max(src_imp × edge_imp / 10)`

    Replaces the buggy `me::importance_rater` Entity-LLM path which
    misrated Emi 3.0 because it categorized her as an "AI tool" via the
    prompt's framing. The edge graph already encodes the truth.

    Edge rating is mandatory (was previously subsystem-gated OFF) because
    derivation now depends on it. Unrated edges contribute None and are
    skipped — derivation is a safe lower bound that improves as the edge
    rater backfills.

    Reads ``spec.batch_size_edges`` for edge rater throughput.
    """
    from app.assistant.importance.scoring import (
        regenerate_edge_importance,
        regenerate_entity_importance,
        regenerate_state_importance,
    )
    from app.assistant.scope.loader import load_scope_for_source
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    batch_edges = int(spec.get("batch_size_edges", 50))
    # Per-run caps so a backlog (bulk import / re-extraction) converges over
    # runs instead of one unbounded LLM grind. Both underlying loops use
    # shrinking selectors (only_unrated edges / untagged nodes), so capping
    # converges. Paired with max_run_seconds in the routine config.
    max_edges = int(spec.get("max_edges_per_run", 400))
    max_tags = int(spec.get("max_tags_per_run", 60))

    # One scope, built at the routine entry, threaded into the two LLM steps
    # (edge rater + section tagger). The pure-DB derivation steps take no scope.
    scope = load_scope_for_source(
        kind="pipeline",
        source_id="kg_importance_rater",
        actor_id="kg_importance_rater",
    )

    # Step 1: rate edges via me::edge_importance_rater. The single LLM input.
    edge_count = regenerate_edge_importance(
        batch_size=batch_edges, only_unrated=True, max_edges=max_edges,
        scope_context=scope,
    )
    edge_status = int(edge_count or 0)

    # Step 2: derive Entity / Concept importance from adjacent edge importance.
    # Pure max; runs AFTER edge rater so inputs are fresh.
    entity_count = regenerate_entity_importance(only_unrated=False)

    # Step 3: derive State / Event / Goal / Property importance via
    # max(src_imp × edge_imp / 10). Depends on Entity importance from step 2.
    state_count = regenerate_state_importance(only_unrated=False)

    # Section tagging — runs AFTER importance rating completes so the tagger
    # can see real importance values (gate by source-entity importance is
    # well-defined). Per the deferred-dependency chain:
    #   promote (NULL imp) → rate → tag → card / wiki dirty-sweep
    # tag_nodes_by_id walks every taggable State/Event/Goal/Property without
    # a tag yet (backfill semantics — idempotent on repeated runs).
    tag_stats = {}
    try:
        from app.assistant.kg.section_tagging import backfill_untagged_nodes
        tag_stats = backfill_untagged_nodes(scope_context=scope, limit=max_tags)
    except Exception:
        logger.exception("[kg_importance_rater] tagging step failed")
        tag_stats = {"error": "exception"}

    return {
        "edges_rated": edge_status,
        "entity_nodes_derived": int(entity_count or 0),
        "state_nodes_derived": int(state_count or 0),
        "tagging": tag_stats,
    }


kg_importance_rater = _lazy_kg_importance_rater


def _lazy_wiki_growth(*, target_date=None, routine=None):
    """Nightly: build up to N new wiki pages for the highest-degree Entity
    nodes that don't yet have one. Pairs with wiki_nightly_refresh —
    refresh maintains existing pages, growth adds new ones over time."""
    from pathlib import Path
    from app.assistant.wiki_generator.growth import run_wiki_growth
    spec = (routine.spec if routine and hasattr(routine, "spec") else {}) or {}
    vault_raw = spec.get("vault_path") or ""
    vault_path = Path(vault_raw) if vault_raw else None
    max_new_pages = int(spec.get("max_new_pages", 5))
    min_degree = int(spec.get("min_degree", 4))
    run_critic = bool(spec.get("run_critic", True))
    return run_wiki_growth(
        vault_path=vault_path,
        max_new_pages=max_new_pages,
        min_degree=min_degree,
        run_critic=run_critic,
    )


wiki_growth = _lazy_wiki_growth


def sleep_camera_tick_local(*, target_date=None, routine=None):
    """Capture one frame from a configured LAN RTSP camera (Tapo / Wyze /
    Reolink etc.) for sleep monitoring. Parallel to ``sleep_camera_tick``
    which uses the Ring bridge — switch to this one when a higher-res
    local camera is in place.

    The camera_alias comes from the routine spec (``spec.camera_alias``).
    The local_camera_snapshot tool handles ffmpeg + event publishing; the
    sleep_analyzer subscriber picks up the resulting frame via the new
    ``kind: "bedroom"`` filter.
    """
    spec = (routine.spec if routine and isinstance(getattr(routine, "spec", None), dict) else {}) or {}
    camera_alias = str(spec.get("camera_alias") or "").strip()
    if not camera_alias:
        logger.error("sleep_camera_tick_local: spec.camera_alias is required.")
        return {"status": "error", "error": "missing camera_alias in routine spec"}
    try:
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.utils.pydantic_classes import ToolMessage
        cls = DI.tool_registry.get_tool_class("local_camera_snapshot")
        if cls is None:
            return {"status": "error", "error": "local_camera_snapshot tool not registered"}
        msg = ToolMessage(
            tool_name="local_camera_snapshot",
            tool_data={"arguments": {"camera_alias": camera_alias}},
        )
        result = cls().execute(msg)
        if getattr(result, "result_type", "") == "error":
            return {"status": "error", "error": getattr(result, "content", "tool failed")}
        data = getattr(result, "data", {}) or {}
        return {
            "status": "ok",
            "snapshot_path": data.get("snapshot_path"),
            "size_bytes": data.get("size_bytes"),
            "camera_alias": camera_alias,
        }
    except Exception as e:
        logger.warning("sleep_camera_tick_local: capture failed (alias %s): %s", camera_alias, e)
        return {"status": "error", "error": str(e), "camera_alias": camera_alias}


ROUTINE_FUNCTION_REGISTRY = {
    "situation_audit": situation_audit,
    "location_refresh": location_refresh,
    "chat_memory_index": chat_memory_index,
    "proposal_promoter": proposal_promoter,
    "wiki_nightly_refresh": wiki_nightly_refresh,
    "wiki_growth": wiki_growth,
    "kg_finding_backlog_drain": kg_finding_backlog_drain,
    "kg_date_gap_drain": kg_date_gap_drain,
    "kg_state_decay": kg_state_decay,
    "kg_goal_dormancy_sweep": kg_goal_dormancy_sweep,
    "kg_goal_outcome_detect": kg_goal_outcome_detect,
    "kg_finding_cluster_resolve": kg_finding_cluster_resolve,
    "kg_finding_executor_drain": kg_finding_executor_drain,
    "kg_dup_cluster_drain": kg_dup_cluster_drain,
    "kg_state_date_drain": kg_state_date_drain,
    "kg_wiki_inference": kg_wiki_inference,
    "kg_importance_rater": kg_importance_rater,
    "entity_card_refresh": entity_card_refresh,
    "kg_identity_sentence_refresh": kg_identity_sentence_refresh,
    "kg_evidence_digestion": kg_evidence_digestion,
    "kg_embedding_diff_sync": kg_embedding_diff_sync,
    "kg_embedding_full_rebuild": kg_embedding_full_rebuild,
    "sleep_camera_tick_local": sleep_camera_tick_local,
}

# Filesystem-discovered handlers from app/assistant/routine_handlers/.
# Drop a new file there decorated with @routine_handler() and it gets
# picked up here without editing this registry. Hand-registered entries
# above win on name collision (legacy wins; auto-discovered drops with
# a warning) so the migration path is "stop registering manually, the
# auto-loader keeps things working."
try:
    from app.assistant.routine_handlers import discover_handlers as _discover_handlers
    for _name, _fn in _discover_handlers().items():
        if _name in ROUTINE_FUNCTION_REGISTRY:
            logger.warning(
                "[routine_functions] auto-discovered handler %r collides with manual registration; "
                "keeping manual",
                _name,
            )
            continue
        ROUTINE_FUNCTION_REGISTRY[_name] = _fn
except Exception as _e:
    logger.warning("[routine_functions] handler auto-discovery failed: %s", _e)
