"""
Step 2: Update beliefs based on collected evidence.

For the evidence batch:
  1. Embed the evidence and find semantically similar existing beliefs — GLOBALLY, across
     every domain, so a duplicate filed under another area is visible to the updater.
  2. Send evidence + existing beliefs to the belief_updater LLM agent. The agent files each
     belief under a primary `domain` (one of the enabled domain ids); domains are areas a
     belief belongs to, not lanes the engine runs in.
  3. Apply the agent's decisions (create / update / deprecate / no_change) via BeliefStore.

Write-time dedup (2026-09-10): before a `create` lands, its statement is compared with its
nearest active belief in the whole store. If they embed at/above MERGE_THRESHOLD the
merge_verifier decides ONCE whether they are the same belief; a "same" verdict turns the
create into an update of the existing key (statement = the verifier's reconciled canonical),
and a "not the same" verdict is recorded in belief_distinct_pairs so the weekly sweep never
re-asks. This is where duplicates used to be born — one lane could not see another lane's
belief — and it costs one verifier call per new belief instead of one per candidate pair.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

from belief_engine.pipeline.steps.canonicalize_belief_set import (
    MERGE_THRESHOLD, _NEW_STATEMENT_CONTEXT, belief_context_block, verify_relation,
)
from belief_engine.pipeline.steps.collect_evidence import EvidenceBundle, EvidenceItem
from belief_engine.store import distinct_pairs as verdicts
from belief_engine.store.belief_store import BeliefRecord, BeliefStore, BeliefUpsertRequest, EvidenceInput

logger = get_logger(__name__)

_AGENT_NAME = "belief_engine::belief_updater"
_VERIFIER_AGENT_NAME = "belief_engine::merge_verifier"

# How many existing beliefs to surface per semantic search.
_SIMILAR_K = 8
_SIMILARITY_THRESHOLD = 0.50
# Nearest neighbours considered when a new belief is about to be created.
_DEDUP_K = 3


def _format_existing_beliefs(beliefs) -> str:
    if not beliefs:
        return "(none)"
    lines = []
    for belief, score in beliefs:
        lines.append(
            f"- key={belief.belief_key!r} domain={belief.domain} confidence={belief.confidence} "
            f"obs={belief.observation_count} similarity={score:.2f}"
        )
        lines.append(f"  Statement: {belief.statement}")
    return "\n".join(lines)


def _to_evidence_input(item: EvidenceItem) -> EvidenceInput:
    return EvidenceInput(
        source_type=item.source_type,
        source_date=item.source_date,
        source_ref=item.source_ref,
        signal_type=item.signal_type,
        summary=item.summary,
        raw_text=item.raw_text,
        weight=item.weight,
        extracted_by="belief_engine::belief_updater",
    )


def _resolve_evidence_by_refs(
    items: List[EvidenceItem], refs: List[int]
) -> List[EvidenceItem]:
    """Resolve 1-based evidence indices (as emitted by the LLM) to EvidenceItem
    objects. Out-of-range indices are logged and skipped. Empty refs → attach
    no evidence (the LLM chose not to cite any)."""
    if not refs:
        return []
    resolved = []
    for ref in refs:
        idx = ref - 1  # convert to 0-based
        if 0 <= idx < len(items):
            resolved.append(items[idx])
        else:
            logger.debug("[UpdateBeliefsStep] evidence_ref %d out of range (batch size=%d)", ref, len(items))
    return resolved


def resolve_domain(
    *,
    agent_domain: Any,
    belief_key: str,
    existing: Optional[BeliefRecord],
    valid_domains: List[str],
    forced: Optional[str] = None,
) -> Optional[str]:
    """The domain a belief write lands in.

    A per-domain slice (`forced`) files everything under that domain, as before. Otherwise an
    existing belief keeps its domain (the updater does not move beliefs between areas), a new
    belief takes the updater's `domain` when it is a known id, else the dot-prefix of its key
    when that is a known id. None means the write cannot be filed and must be refused loudly.
    """
    if forced:
        return forced
    if existing is not None:
        return existing.domain
    cand = str(agent_domain or "").strip().lower()
    if cand in valid_domains:
        return cand
    prefix = belief_key.split(".", 1)[0].strip().lower() if belief_key else ""
    if prefix in valid_domains:
        return prefix
    return None


def dedup_candidate(
    store: BeliefStore,
    verifier_agent: Any,
    *,
    statement: str,
    belief_key: str,
    scope_context: Any,
) -> Tuple[Optional[BeliefRecord], Optional[str], List[Tuple[BeliefRecord, str]]]:
    """Decide whether a NEW statement is really an existing belief.

    Returns (existing_belief, canonical_statement, distinct_verdicts):
      - existing_belief is the active belief the verifier judged to be the SAME (the create
        becomes an update of it), else None;
      - canonical_statement is the verifier's reconciled text for that merge (may be empty);
      - distinct_verdicts lists (candidate, reason) pairs the verifier judged NOT the same, for
        the caller to record once the new belief has an id.
    One verifier call per candidate, nearest first, stopping at the first "same". A
    `supersedes` verdict deprecates whichever side the evidence dates as outdated, and a
    `contradicts` verdict contests the stored belief so the reevaluator rules on its full
    trail; both let the new belief proceed.
    """
    hits = store.find_similar(statement, k=_DEDUP_K, threshold=MERGE_THRESHOLD)
    distinct: List[Tuple[BeliefRecord, str]] = []
    for cand, score in hits:
        if cand.belief_key == belief_key or getattr(cand, "locked", 0):
            continue
        # A locked belief must never be rewritten by a merge; a same-key hit is an update, not a dup.
        verdict = verify_relation(
            verifier_agent,
            _AsBelief(statement, belief_key), cand, scope_context=scope_context,
            context_a=_NEW_STATEMENT_CONTEXT,
            context_b=belief_context_block(store, cand),
        )
        relation = verdict["relation"]
        reason = str(verdict.get("reason") or "")

        if relation == "same":
            logger.info(
                "[UpdateBeliefsStep] write-time dedup: %s folds into %s (sim=%.2f): %s",
                belief_key, cand.belief_key, score, reason[:160],
            )
            return cand, (verdict.get("canonical_statement") or "").strip(), distinct

        if relation == "supersedes":
            # The incoming statement is side 'a'. If the evidence says the STORED belief is
            # the outdated one, this new belief replaces it — deprecate the old rather than
            # letting a preference and its own replacement both sit active. If the stored one
            # is current instead, the "new" belief is stale news; let it be written anyway
            # (its own evidence dates it) rather than silently discarding what was observed.
            if str(verdict.get("current_side") or "").strip().lower() == "a":
                try:
                    store.deprecate(cand.belief_key,
                                    reason=f"superseded by new belief {belief_key}: {reason[:200]}")
                    logger.info("[UpdateBeliefsStep] %s supersedes %s — old one deprecated",
                                belief_key, cand.belief_key)
                except Exception:
                    logger.exception("[UpdateBeliefsStep] deprecate failed for %s", cand.belief_key)
            else:
                logger.warning(
                    "[UpdateBeliefsStep] %s conflicts with %s but the STORED belief reads as "
                    "current; writing the new one anyway for its own evidence. %s",
                    belief_key, cand.belief_key, reason[:160])
            continue

        if relation == "contradicts":
            # Same subject, opposing claims, nothing dates them apart. Contest the stored side
            # so the reevaluator rules on its full trail; the new belief is still written, and
            # its evidence is what the reevaluator will weigh against.
            try:
                store.mark_contested(cand.belief_key)
            except Exception:
                logger.exception("[UpdateBeliefsStep] mark_contested failed for %s", cand.belief_key)
            logger.warning(
                "[UpdateBeliefsStep] CONTRADICTION: new %s vs stored %s — stored one contested. %s",
                belief_key, cand.belief_key, reason[:200])
            continue

        # different | specialises — both stand; remember the verdict once the new belief has an id.
        distinct.append((cand, f"{relation}: {reason}"))
    return None, None, distinct


class _AsBelief:
    """The minimal shape verify_relation reads for the new side: statement + key. Its context
    block is _NEW_STATEMENT_CONTEXT (nothing stored yet), so no evidence lookup is attempted."""
    def __init__(self, statement: str, belief_key: str) -> None:
        self.statement = statement
        self.belief_key = belief_key


class UpdateBeliefsStep:
    name = "update_beliefs"

    def __init__(self, domain: Optional[str] = None) -> None:
        # domain=None is the global pass. A named domain is a per-domain slice: evidence,
        # similarity, and filing all stay within it (scripts / inspection).
        if domain is not None:
            from belief_engine.config import get_domain_config
            if get_domain_config(domain) is None:
                raise ValueError(
                    f"Unknown belief-engine domain {domain!r} — no entry in domain config."
                )
        self.domain = domain

    def inputs(self, ctx: Any) -> list:
        return ["evidence_bundle from collect_evidence", "db: user_beliefs"]

    def outputs(self, ctx: Any) -> list:
        return []

    def run(self, ctx: Any) -> dict:
        label = self.domain or "global"
        bundle: Optional[EvidenceBundle] = getattr(ctx, "evidence_bundle", None)
        if bundle is None or bundle.is_empty():
            logger.info("[UpdateBeliefsStep] no evidence for %s — skipping", label)
            ctx.belief_update_result = {"status": "skipped", "reason": "no_evidence"}
            return ctx.belief_update_result

        from belief_engine.config import list_all_domain_ids
        valid_domains = [d.lower() for d in list_all_domain_ids()]

        store = BeliefStore()

        agent_factory = ServiceLocator.get("agent_factory")
        if agent_factory is None:
            raise RuntimeError("agent_factory not available in DI")
        agent = agent_factory.create_agent(_AGENT_NAME)
        if agent is None:
            raise RuntimeError(f"Agent {_AGENT_NAME!r} not found")
        verifier = agent_factory.create_agent(_VERIFIER_AGENT_NAME)
        if verifier is None:
            raise RuntimeError(f"Agent {_VERIFIER_AGENT_NAME!r} not found")

        items = bundle.items
        evidence_block = bundle.as_block()

        # One semantic query over the whole evidence batch — coarse but cheap;
        # surfaces beliefs related to the overall topic mix rather than each
        # individual item. Global unless this is a per-domain slice.
        combined_query = " ".join(item.summary for item in items)
        existing_hits = store.find_similar(
            combined_query,
            k=_SIMILAR_K,
            domain=self.domain,
            threshold=_SIMILARITY_THRESHOLD,
        )
        existing_block = _format_existing_beliefs(existing_hits)

        # Scope built once by the pipeline, threaded via ctx — this LLM step
        # receives it, does not build its own.
        msg = Message(
            agent_input={
                "task": (f"Process new evidence for the '{self.domain}' domain."
                         if self.domain else "Process new evidence across all areas."),
                "evidence_block": evidence_block,
                "existing_beliefs_block": existing_block,
                "date_today": get_local_time_str(),
                "domains": ", ".join(valid_domains),
            },
            scope_context=ctx.scope_context,
        )

        resp = agent.action_handler(msg)
        payload = resp.data if resp and hasattr(resp, "data") else {}
        belief_outputs = payload.get("beliefs") or []

        stats = {"created": 0, "updated": 0, "deprecated": 0, "no_change": 0, "errors": 0,
                 "contested": 0, "dedup_merged": 0, "verifier_calls": 0}
        # Beliefs that need re-evaluation (confidence dropped or explicitly contested)
        contested_keys: List[str] = []
        today_iso = datetime.now(timezone.utc).date().isoformat()

        _CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

        verdict_conn = verdicts.open_conn()
        try:
            for bo in belief_outputs:
                try:
                    action = bo.get("action", "no_change")
                    belief_key = bo.get("belief_key", "")
                    if not belief_key:
                        logger.warning("[UpdateBeliefsStep] skipping belief with empty key")
                        continue

                    existing = store.get_by_key(belief_key)

                    # Honor a manual lock: a belief the owner corrected + locked via /beliefs must
                    # not be re-evolved or deprecated. Downgrade a mutating action to no_change so
                    # evidence still attaches and observation_count/last_confirmed advance, but the
                    # statement and status stay exactly as the owner set them.
                    if action in ("update", "deprecate") and existing and getattr(existing, "locked", 0):
                        logger.info(
                            "[UpdateBeliefsStep] %s is LOCKED — preserving manual correction (was action=%s)",
                            belief_key, action,
                        )
                        action = "no_change"

                    if action == "deprecate":
                        store.deprecate(belief_key, reason=bo.get("reasoning", ""))
                        stats["deprecated"] += 1
                        continue

                    if action == "no_change":
                        # Still upsert to increment observation_count and attach evidence.
                        if existing:
                            req = BeliefUpsertRequest(
                                domain=existing.domain,
                                belief_key=belief_key,
                                statement=existing.statement,
                                confidence=existing.confidence,
                                scope=existing.scope,
                                status=existing.status,
                                last_confirmed=today_iso,
                            )
                            refs = bo.get("evidence_refs") or []
                            relevant_ev = _resolve_evidence_by_refs(items, refs)
                            store.upsert_belief(req, [_to_evidence_input(ev) for ev in relevant_ev])
                            stats["no_change"] += 1
                        continue

                    new_confidence = bo.get("confidence", "medium")
                    new_status = bo.get("status", "active")
                    new_statement = (bo.get("statement") or "").strip()

                    # Refuse to upsert an empty statement — would blank the belief
                    # in the store. The agent_form requires statement; this guards
                    # against degenerate output.
                    if not new_statement:
                        logger.warning(
                            "[UpdateBeliefsStep] empty statement on %s action=%s — skipping",
                            belief_key, action,
                        )
                        continue

                    # An agent that says "create" for a key that already exists is updating it.
                    if action == "create" and existing is not None:
                        action = "update"

                    # Write-time dedup: is this "new" belief an existing one in other words?
                    distinct_to_record: List[Tuple[BeliefRecord, str]] = []
                    if action == "create":
                        folded, canonical, distinct_to_record = dedup_candidate(
                            store, verifier,
                            statement=new_statement, belief_key=belief_key,
                            scope_context=ctx.scope_context,
                        )
                        stats["verifier_calls"] += len(distinct_to_record) + (1 if folded else 0)
                        if folded is not None:
                            action = "update"
                            existing = folded
                            belief_key = folded.belief_key
                            new_statement = canonical or new_statement
                            stats["dedup_merged"] += 1

                    domain = resolve_domain(
                        agent_domain=bo.get("domain"), belief_key=belief_key,
                        existing=existing, valid_domains=valid_domains, forced=self.domain,
                    )
                    if domain is None:
                        stats["errors"] += 1
                        logger.error(
                            "[UpdateBeliefsStep] cannot file %s: domain %r is not an enabled domain "
                            "and the key carries no known prefix — skipped",
                            belief_key, bo.get("domain"),
                        )
                        continue

                    prev_confidence = existing.confidence if (existing and action == "update") else new_confidence

                    req = BeliefUpsertRequest(
                        domain=domain,
                        belief_key=belief_key,
                        statement=new_statement,
                        confidence=new_confidence,
                        scope=bo.get("scope", "chronic"),
                        status=new_status,
                        last_confirmed=today_iso,
                        kind=bo.get("kind"),
                    )
                    refs = bo.get("evidence_refs") or []
                    relevant_ev = _resolve_evidence_by_refs(items, refs)
                    evidence_inputs = [_to_evidence_input(ev) for ev in relevant_ev]
                    written = store.upsert_belief(req, evidence_inputs)
                    logger.debug(
                        "[UpdateBeliefsStep] attached %d/%d evidence items to %s (refs=%s)",
                        len(relevant_ev), len(items), belief_key, refs,
                    )

                    # The new belief now has an id: remember which neighbours it is NOT, so the
                    # weekly sweep never pays for those pairs again.
                    for cand, reason in distinct_to_record:
                        verdicts.record_distinct(
                            verdict_conn, written.id, written.statement, cand.id, cand.statement,
                            reason=reason,
                        )

                    # Flag for re-evaluation if:
                    # 1. Status was set to contested (applies to BOTH create and update —
                    #    a fresh-but-contested belief needs the reevaluator too), OR
                    # 2. Confidence dropped from a known previous value (update only).
                    if new_status == "contested":
                        contested_keys.append(belief_key)
                        store.mark_contested(belief_key)
                        stats["contested"] += 1
                        logger.info("[UpdateBeliefsStep] marked contested: %s", belief_key)
                    elif action == "update" and _CONFIDENCE_RANK.get(new_confidence, 1) < _CONFIDENCE_RANK.get(prev_confidence, 1):
                        contested_keys.append(belief_key)
                        store.mark_contested(belief_key)
                        stats["contested"] += 1
                        logger.info(
                            "[UpdateBeliefsStep] confidence drop %s→%s on %s → queued for re-eval",
                            prev_confidence, new_confidence, belief_key,
                        )

                    if action == "create":
                        stats["created"] += 1
                    else:
                        stats["updated"] += 1

                    logger.info(
                        "[UpdateBeliefsStep] %s belief key=%s domain=%s confidence=%s",
                        action, belief_key, domain, req.confidence,
                    )
                except Exception as exc:
                    stats["errors"] += 1
                    logger.exception(
                        "[UpdateBeliefsStep] failed processing belief key=%s: %s",
                        bo.get("belief_key", "?"), exc,
                    )
        finally:
            verdict_conn.close()

        logger.info("[UpdateBeliefsStep] %s stats=%s", label, stats)
        ctx.belief_update_result = {
            "status": "partial_error" if stats["errors"] > 0 else "ok",
            "domain": label,
            "stats": stats,
            "contested_keys": contested_keys,
        }
        return ctx.belief_update_result
