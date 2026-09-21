"""Update beliefs from source evidence and LLM-selected existing context.
New records are preserved first; the subsequent incremental matcher reviews complete
stored records and evidence. No statement-only write-time merge is performed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, List, Optional

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.time_utils import get_local_time_str

from belief_engine.pipeline.steps.collect_evidence import EvidenceBundle, EvidenceItem
from belief_engine.matching.service import select_for_evidence
from belief_engine.matching.context import encode, pages
from dataclasses import asdict
from belief_engine.store.belief_store import BeliefRecord, BeliefStore, BeliefUpsertRequest, EvidenceInput

logger = get_logger(__name__)

_AGENT_NAME = "belief_engine::belief_updater"

# Reserve ample space for system resources, schema and the structured response.
# This is a conservative transport budget, not a text truncation limit.
UPDATE_INPUT_BYTES = 180000

class UpdateContextTooLarge(ValueError):
    pass

def check_update_budget(payload):
    size = len(encode(payload).encode('utf-8'))
    if size > UPDATE_INPUT_BYTES:
        raise UpdateContextTooLarge(f'Complete update input needs {size} UTF-8 bytes; budget is {UPDATE_INPUT_BYTES}. Source remains pending; no content was truncated.')
    return size

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


def _assessed_evidence(items: List[EvidenceItem], output: dict) -> List[EvidenceInput]:
    """Validate model-assigned relationships; never infer them from event sentiment."""
    refs = output.get('evidence_refs') or []
    relations = output.get('evidence_relations') or []
    by_ref = {}
    for relation in relations:
        ref = relation['evidence_ref']
        if type(ref) is not int or ref in by_ref or ref < 1 or ref > len(items):
            raise ValueError('Invalid or repeated evidence relation')
        if relation['valence'] not in ('support','contradict','qualify'):
            raise ValueError('Invalid evidence valence')
        by_ref[ref] = relation['valence']
    if any(type(ref) is not int or ref < 1 or ref > len(items) for ref in refs):
        raise ValueError('Evidence reference outside supplied batch')
    if set(refs) != set(by_ref):
        raise ValueError('Each cited source requires an explicit evidence-to-claim relationship')
    result = []
    for ref in dict.fromkeys(refs):
        evidence = _to_evidence_input(items[ref - 1])
        evidence.valence = by_ref[ref]
        result.append(evidence)
    return result


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
        bundle = getattr(ctx, "evidence_bundle", None)
        if bundle is None or bundle.is_empty():
            ctx.belief_update_result = {"status": "skipped", "reason": "no_evidence"}
            return ctx.belief_update_result
        # Whole evidence records, chronological so a later correction is processed
        # after the earlier observation. Paging is transport, never a semantic bucket.
        from dataclasses import replace
        from types import SimpleNamespace
        total = {k: 0 for k in ("created", "updated", "deprecated", "no_change", "errors", "contested")}
        contested = []
        batches = 0
        def process(items):
            nonlocal batches
            child = SimpleNamespace(scope_context=ctx.scope_context,
                                    evidence_bundle=replace(bundle, items=items))
            try:
                result = self._run_batch(child)
            except UpdateContextTooLarge:
                if len(items) == 1:
                    raise
                middle = len(items) // 2
                process(items[:middle])
                process(items[middle:])
                return
            batches += 1
            for key, value in result.get('stats', {}).items():
                total[key] += value
            contested.extend(result.get('contested_keys', []))
            if total['errors']:
                raise RuntimeError(f"Belief update batch failed: {result}")
        records = sorted(bundle.items, key=lambda item: (item.source_date, item.source_ref or ''))
        for page in pages([{'position':i,'item':asdict(item)} for i,item in enumerate(records)], max_chars=48000):
            process([records[r['position']] for r in page])
        ctx.belief_update_result = {'status':'ok','domain':self.domain or 'global',
                                  'stats':total,'contested_keys':list(dict.fromkeys(contested)),
                                  'batches':batches}
        return ctx.belief_update_result

    def _run_batch(self, ctx: Any) -> dict:
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
        items = bundle.items
        evidence_block = bundle.as_block()

        existing_block = encode(select_for_evidence(store, agent_factory, ctx.scope_context,
                                                     [asdict(item) for item in items]))

        # Scope built once by the pipeline, threaded via ctx — this LLM step
        # receives it, does not build its own.
        msg = Message(
            agent_input={
                "update_domain": self.domain,
                "evidence_block": evidence_block,
                "existing_beliefs_block": existing_block,
                "date_today": get_local_time_str(),
                "domains": ", ".join(valid_domains),
            },
            scope_context=ctx.scope_context,
        )

        input_bytes = check_update_budget(msg.agent_input)
        logger.info("[UpdateBeliefsStep] bounded input bytes=%d evidence_items=%d", input_bytes, len(items))
        resp = agent.action_handler(msg)
        payload = resp.data if resp and hasattr(resp, "data") else {}
        belief_outputs = payload.get("beliefs") or []

        stats = {"created": 0, "updated": 0, "deprecated": 0, "no_change": 0, "errors": 0,
                 "contested": 0}
        # Beliefs that need re-evaluation (confidence dropped or explicitly contested)
        contested_keys: List[str] = []
        today_iso = datetime.now(timezone.utc).date().isoformat()

        _CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}

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
                # evidence still attaches and source-derived metadata is refreshed, but the
                # statement and status stay exactly as the owner set them.
                if action in ("create", "replace", "update", "deprecate") and existing and getattr(existing, "locked", 0):
                    logger.info(
                        "[UpdateBeliefsStep] %s is LOCKED — preserving manual correction (was action=%s)",
                        belief_key, action,
                    )
                    action = "no_change"

                if action == "deprecate":
                    assessed = _assessed_evidence(items, bo)
                    if existing and assessed:
                        store.add_evidence_to_existing(belief_key, assessed)
                    store.deprecate(belief_key, reason=bo.get("reasoning", ""))
                    stats["deprecated"] += 1
                    continue

                if action == "no_change":
                    # Attach newly cited evidence; rereading does not increment or reconfirm.
                    if existing:
                        req = BeliefUpsertRequest(
                            domain=existing.domain,
                            belief_key=belief_key,
                            statement=existing.statement,
                            confidence=existing.confidence,
                            scope=existing.scope,
                            status=existing.status,
                            conditions=existing.conditions,
                            last_confirmed=today_iso,
                        )
                        refs = bo.get("evidence_refs") or []
                        relevant_ev = _resolve_evidence_by_refs(items, refs)
                        store.upsert_belief(req, _assessed_evidence(items, bo))
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
                    conditions=json.loads(bo["conditions_json"]) if bo.get("conditions_json") is not None else (existing.conditions if existing else None),
                )
                refs = bo.get("evidence_refs") or []
                relevant_ev = _resolve_evidence_by_refs(items, refs)
                evidence_inputs = _assessed_evidence(items, bo)
                written = store.upsert_belief(req, evidence_inputs)
                logger.debug(
                    "[UpdateBeliefsStep] attached %d/%d evidence items to %s (refs=%s)",
                    len(relevant_ev), len(items), belief_key, refs,
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

        logger.info("[UpdateBeliefsStep] %s stats=%s", label, stats)
        ctx.belief_update_result = {
            "status": "partial_error" if stats["errors"] > 0 else "ok",
            "domain": label,
            "stats": stats,
            "contested_keys": contested_keys,
        }
        return ctx.belief_update_result
