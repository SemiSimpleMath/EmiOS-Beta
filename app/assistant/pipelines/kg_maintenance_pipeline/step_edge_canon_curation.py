"""Step: edge_canon_curation

Periodic LLM-driven growth of the edge-predicate vocabulary.

Strategy (per user direction 2026-05-12):
  Phase A — accumulate. Write-time normalize_predicate handles known
            aliases; novel predicates pass through and are written raw.
  Phase B — sweep. This step:
            1. finds novel predicates with count >= MIN_COUNT in the live
               graph that aren't in edge_canon or edge_alias.
            2. for each, gathers sample sentences + embedding-similar
               existing canons.
            3. calls me::edge_canon_curator with the batch.
            4. applies verdicts:
                 - variant_of  → INSERT edge_alias; rewrite
                                 kg_edge_metadata.relationship_type to canon
                 - new_canon   → INSERT edge_canon
                 - not_yet     → no-op (revisit next sweep)
            5. resets normalize_predicate's DB cache so the next write-time
               call picks up newly-added aliases.

Threshold: predicates with fewer than MIN_COUNT edges stay raw — let them
accumulate evidence first. Singletons + doubletons are out of scope.

Session / LLM contract: NO session held across LLM calls.
"""
from __future__ import annotations

from typing import Dict, List

from app.assistant.pipelines.context import PipelineContext
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)


MIN_COUNT = 3
MAX_CANDIDATES_PER_RUN = 50  # cap to bound LLM cost per sweep
SAMPLE_SENTENCES_PER_PREDICATE = 4
SIMILAR_CANONS_PER_PREDICATE = 5
BATCH_SIZE = 15  # predicates per LLM call


def run(ctx: PipelineContext = None) -> dict:
    """Run one curation sweep. Returns stats dict."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import (
        Message, ScopeApprovalPolicy, ScopeContext, ScopeResourcePolicy,
    )

    # --- Phase 1: find candidates (read session, no LLM) ---
    with get_db_manager().read_session() as session:
        from sqlalchemy import text as sql_text
        candidate_rows = session.execute(sql_text(f"""
            SELECT em.relationship_type, COUNT(*) as cnt
            FROM kg_edge_metadata em
            WHERE em.relationship_type NOT IN (SELECT edge_type FROM edge_canon)
              AND em.relationship_type NOT IN (SELECT raw_text FROM edge_alias)
            GROUP BY em.relationship_type
            HAVING cnt >= {MIN_COUNT}
            ORDER BY cnt DESC
            LIMIT {MAX_CANDIDATES_PER_RUN}
        """)).fetchall()

    if not candidate_rows:
        logger.info("[edge_canon_curation] no candidates above threshold")
        return {"candidates": 0, "verdicts": 0, "alias_added": 0, "canon_added": 0, "edges_rewritten": 0}

    logger.info(
        "[edge_canon_curation] %d candidate predicates (count >= %d)",
        len(candidate_rows), MIN_COUNT,
    )

    # Gather sample sentences + similar canons per predicate
    candidates: List[Dict] = []
    canon_labels = _load_canon_labels()

    with get_db_manager().read_session() as session:
        for predicate, count in candidate_rows:
            samples = session.execute(sql_text(f"""
                SELECT sentence FROM kg_edge_metadata
                WHERE relationship_type = :p
                  AND sentence IS NOT NULL AND sentence != ''
                LIMIT {SAMPLE_SENTENCES_PER_PREDICATE}
            """), {"p": predicate}).fetchall()
            sample_sentences = [s[0] for s in samples if s[0]]
            candidates.append({
                "predicate": predicate,
                "count": count,
                "sample_sentences": sample_sentences,
                "nearby_canons": canon_labels,  # let the agent see the full list for now
            })

    # --- Phase 2: call curator agent (no DB session held) ---
    agent = DI.agent_factory.create_agent("me::edge_canon_curator")
    if agent is None:
        logger.warning("[edge_canon_curation] curator agent unavailable")
        return {"candidates": len(candidates), "verdicts": 0, "alias_added": 0, "canon_added": 0, "edges_rewritten": 0}

    scope = ScopeContext(
        scope_id="scope::edge_canon_curation",
        owner_id="jukka",
        actor_id="edge_canon_curator_runner",
        surface="ui",
        room_id="me_lens",
        approval=ScopeApprovalPolicy(authority_level=99),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )

    all_verdicts: List[Dict] = []
    for i in range(0, len(candidates), BATCH_SIZE):
        batch = candidates[i:i+BATCH_SIZE]
        batch_text = _format_batch_for_agent(batch)
        try:
            result = agent.action_handler(Message(
                agent_input={"task": batch_text, "information": ""},
                task=batch_text, information="",
                scope_context=scope,
            ))
            data = getattr(result, "data", None) or {}
            if hasattr(data, "model_dump"):
                data = data.model_dump()
            for v in (data.get("verdicts") or []):
                if isinstance(v, dict):
                    all_verdicts.append(v)
        except Exception as exc:
            logger.warning("[edge_canon_curation] batch %d failed: %s", i // BATCH_SIZE + 1, exc)

    logger.info("[edge_canon_curation] got %d verdicts", len(all_verdicts))

    # --- Phase 3: apply verdicts (transaction) ---
    stats = _apply_verdicts(all_verdicts)

    # --- Phase 4: reset normalize_predicate DB cache ---
    try:
        from app.assistant.kg.predicate_vocabulary import reset_db_alias_cache
        reset_db_alias_cache()
    except Exception as exc:
        logger.warning("[edge_canon_curation] cache reset failed: %s", exc)

    stats["candidates"] = len(candidates)
    stats["verdicts"] = len(all_verdicts)
    logger.info("[edge_canon_curation] done: %s", stats)
    return stats


def _load_canon_labels() -> List[str]:
    """Return the list of existing canon edge_types for the agent to consider."""
    with get_db_manager().read_session() as session:
        from sqlalchemy import text as sql_text
        rows = session.execute(sql_text("SELECT edge_type FROM edge_canon ORDER BY edge_type")).fetchall()
    return [r[0] for r in rows]


def _format_batch_for_agent(batch: List[Dict]) -> str:
    """Render a candidate batch into the prompt-friendly text the curator sees."""
    lines = []
    for c in batch:
        lines.append(f"## predicate: {c['predicate']!r}")
        lines.append(f"   count: {c['count']}")
        lines.append("   sample sentences:")
        for s in c["sample_sentences"][:SAMPLE_SENTENCES_PER_PREDICATE]:
            lines.append(f"     - {s}")
        lines.append("")
    lines.append("## existing canonical edge types (use these as the alias targets):")
    for canon in batch[0]["nearby_canons"]:
        lines.append(f"  - {canon}")
    return "\n".join(lines)


def _apply_verdicts(verdicts: List[Dict]) -> Dict[str, int]:
    """Apply curator verdicts to edge_canon, edge_alias, and kg_edge_metadata."""
    import uuid
    alias_added = 0
    canon_added = 0
    edges_rewritten = 0

    with get_db_manager().transaction(op="edge_canon_curation_apply") as session:
        from sqlalchemy import text as sql_text

        # Load canon types for validation
        canon_rows = session.execute(sql_text(
            "SELECT id, edge_type FROM edge_canon"
        )).fetchall()
        canon_by_type = {r[1]: r[0] for r in canon_rows}

        # Existing aliases (to skip dupes)
        existing_aliases = {
            r[0] for r in session.execute(sql_text(
                "SELECT raw_text FROM edge_alias"
            )).fetchall()
        }

        for v in verdicts:
            predicate = (v.get("predicate") or "").strip()
            verdict = (v.get("verdict") or "").strip().lower()
            target = (v.get("canonical_target") or "").strip()
            if not predicate or not verdict:
                continue

            if verdict == "variant_of":
                if not target or target not in canon_by_type:
                    logger.warning(
                        "[edge_canon_curation] variant_of with unknown target: %r → %r",
                        predicate, target,
                    )
                    continue
                if predicate in existing_aliases:
                    continue
                canon_id = canon_by_type[target]
                session.execute(sql_text("""
                    INSERT INTO edge_alias (id, canon_id, raw_text)
                    VALUES (:id, :canon, :raw)
                """), {"id": str(uuid.uuid4()), "canon": canon_id, "raw": predicate})
                alias_added += 1
                # Drop variant edges that would collide with an existing canonical
                # edge on the same (source, target) pair. The unique constraint
                # (source_id, target_id, relationship_type) means we can't have
                # both. Canonical wins; variant's evidence is already captured
                # in claim_proposal_evidence + kg_edge_evidence, so the merge
                # cascade preserves provenance.
                conflict_delete = session.execute(sql_text("""
                    DELETE FROM kg_edge_metadata
                    WHERE relationship_type = :raw
                      AND EXISTS (
                        SELECT 1 FROM kg_edge_metadata e2
                        WHERE e2.source_id = kg_edge_metadata.source_id
                          AND e2.target_id = kg_edge_metadata.target_id
                          AND e2.relationship_type = :canon
                      )
                """), {"raw": predicate, "canon": target})
                # Rewrite the rest
                result = session.execute(sql_text("""
                    UPDATE kg_edge_metadata SET relationship_type = :canon
                    WHERE relationship_type = :raw
                """), {"canon": target, "raw": predicate})
                edges_rewritten += result.rowcount or 0
                logger.info(
                    "[edge_canon_curation] alias %r → %r (rewrote %d, dropped %d duplicates)",
                    predicate, target, result.rowcount or 0, conflict_delete.rowcount or 0,
                )

            elif verdict == "new_canon":
                canonical_name = target or predicate
                if canonical_name in canon_by_type:
                    continue
                new_canon_id = str(uuid.uuid4())
                session.execute(sql_text("""
                    INSERT INTO edge_canon (id, edge_type, domain_type, range_type, category, description)
                    VALUES (:id, :et, 'Entity', 'State', 'promoted', :desc)
                """), {
                    "id": new_canon_id,
                    "et": canonical_name,
                    "desc": f"Promoted from raw predicate (reason: {v.get('reason', '')[:200]})",
                })
                canon_by_type[canonical_name] = new_canon_id
                canon_added += 1
                # If the predicate string differs from the canonical, alias it
                if predicate != canonical_name and predicate not in existing_aliases:
                    session.execute(sql_text("""
                        INSERT INTO edge_alias (id, canon_id, raw_text)
                        VALUES (:id, :canon, :raw)
                    """), {"id": str(uuid.uuid4()), "canon": new_canon_id, "raw": predicate})
                    alias_added += 1
                # Rewrite predicate string in the edges to canonical form if different.
                # Same conflict-drop pattern as variant_of path.
                if predicate != canonical_name:
                    session.execute(sql_text("""
                        DELETE FROM kg_edge_metadata
                        WHERE relationship_type = :raw
                          AND EXISTS (
                            SELECT 1 FROM kg_edge_metadata e2
                            WHERE e2.source_id = kg_edge_metadata.source_id
                              AND e2.target_id = kg_edge_metadata.target_id
                              AND e2.relationship_type = :canon
                          )
                    """), {"raw": predicate, "canon": canonical_name})
                    result = session.execute(sql_text("""
                        UPDATE kg_edge_metadata SET relationship_type = :canon
                        WHERE relationship_type = :raw
                    """), {"canon": canonical_name, "raw": predicate})
                    edges_rewritten += result.rowcount or 0
                logger.info("[edge_canon_curation] promoted new canon %r", canonical_name)

            # 'not_yet' is no-op

        session.commit()

    return {
        "alias_added": alias_added,
        "canon_added": canon_added,
        "edges_rewritten": edges_rewritten,
    }
