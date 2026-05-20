"""Persist romantic_proposer output as intention pods.

Mirrors wellness_persist shape:
- Each RomanticProposal becomes one pod of kind=`intention.romantic`
- One summary pod of kind=`intention.romantic_set` per run

No calendar writes from this layer. When the calendar projection button
ships (mirroring the meal calendar approach), it'll read these pods and
write to the Food calendar (for date_night_out events) tagged with
created_by="emi/romantic_proposer".
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_romantic_proposer_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Mint pods from a romantic_proposer run."""
    store = PodStore()
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    proposal_pod_ids: List[str] = []

    proposals = output.get("proposals") or []
    skipped_today = output.get("skipped_today") or []
    free_form_thinking = (output.get("free_form_thinking") or "").strip()

    for prop in proposals:
        pod_id = _mint_intention_romantic_pod(store, prop, now_utc_iso)
        if pod_id:
            proposal_pod_ids.append(pod_id)

    set_pod_id = _mint_intention_romantic_set_pod(
        store=store,
        proposal_pod_ids=proposal_pod_ids,
        free_form_thinking=free_form_thinking,
        skipped_today=skipped_today,
        now_utc_iso=now_utc_iso,
    )

    return {
        "proposal_pod_count": len(proposal_pod_ids),
        "proposal_pod_ids": proposal_pod_ids,
        "set_pod_id": set_pod_id,
        "skipped_count": len(skipped_today),
    }


def _mint_intention_romantic_pod(
    store: PodStore,
    proposal: Dict[str, Any],
    now_utc_iso: str,
) -> Optional[str]:
    """One pod per proposed romantic intention."""
    try:
        pod_id = f"datapod:intention.romantic:{uuid.uuid4().hex[:24]}"
        kind = proposal.get("kind") or "romantic"
        actors = proposal.get("actors") or []
        date = proposal.get("date") or ""
        summary = proposal.get("summary") or "(unnamed)"
        source = proposal.get("source") or "?"
        confidence = proposal.get("confidence") or "?"
        novelty = proposal.get("novelty") or "?"
        addresses = proposal.get("addresses_concern_ids") or []
        cost = proposal.get("cost_estimate_usd")
        babysitter = bool(proposal.get("requires_babysitter"))
        advance_days = proposal.get("advance_required_days")
        duration = proposal.get("duration_minutes")
        start_local = proposal.get("proposed_start_local")

        one_liner = f"{kind} {date}: {summary}"
        body_parts = [
            f"# Proposed romantic — {kind} on {date}",
            "",
            f"**Summary:** {summary}",
            f"**Actors:** {', '.join(actors) if actors else '(none specified)'}",
            f"**Source:** {source}  •  **Novelty:** {novelty}  •  **Confidence:** {confidence}",
        ]
        if start_local:
            body_parts.append(f"**Time:** {start_local}")
        if duration is not None:
            body_parts.append(f"**Duration:** {duration} min")
        if cost is not None:
            body_parts.append(f"**Est. cost (USD):** {cost}")
        if babysitter:
            body_parts.append(f"**Requires babysitter:** yes")
        if advance_days is not None:
            body_parts.append(f"**Advance required:** {advance_days} days")

        reasoning = (proposal.get("reasoning") or "").strip()
        if reasoning:
            body_parts += ["", "**Reasoning:**", reasoning]

        novelty_rationale = (proposal.get("novelty_rationale") or "").strip()
        if novelty_rationale:
            body_parts += ["", "**Why this novel pick:**", novelty_rationale]

        if addresses:
            body_parts += ["", "**Addresses concerns:** " + ", ".join(addresses)]

        body = "\n".join(body_parts)
        tags = ["intention", "romantic", kind]
        if novelty == "novel":
            tags.append("novel")
        if babysitter:
            tags.append("needs_sitter")

        pod = Pod(
            pod_id=pod_id,
            kind="intention.romantic",
            tags=tags,
            one_liner=one_liner,
            body=body,
            source_refs=[],  # concern_ids + intra-pod refs live in metadata
            for_agents=[],
            scope_id=None,
            created_by="romantic_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "kind": kind,
                "actors": actors,
                "date": date,
                "summary": summary,
                "source": source,
                "novelty": novelty,
                "confidence": confidence,
                "cost_estimate_usd": cost,
                "requires_babysitter": babysitter,
                "advance_required_days": advance_days,
                "duration_minutes": duration,
                "proposed_start_local": start_local,
                "addresses_concern_ids": addresses,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[romantic_persist] mint intention.romantic failed: %s", e)
        return None


def _mint_intention_romantic_set_pod(
    *,
    store: PodStore,
    proposal_pod_ids: List[str],
    free_form_thinking: str,
    skipped_today: List[str],
    now_utc_iso: str,
) -> Optional[str]:
    """Aggregate pod per romantic_proposer run — captures the narrative +
    references to all per-intention pods."""
    try:
        pod_id = f"datapod:intention.romantic_set:{uuid.uuid4().hex[:24]}"

        body_parts = [
            f"# Romantic proposer set — {now_utc_iso}",
            "",
            f"**Proposals:** {len(proposal_pod_ids)}",
        ]
        if skipped_today:
            body_parts += ["", "**Skipped (deliberately):**"] + [f"- {s}" for s in skipped_today]
        if free_form_thinking:
            body_parts += ["", "**Free-form thinking:**", free_form_thinking]
        if proposal_pod_ids:
            body_parts += ["", "**Per-intention pods:**"] + [f"- {pid}" for pid in proposal_pod_ids]

        one_liner_parts = [f"{len(proposal_pod_ids)} romantic intentions"]
        if skipped_today:
            one_liner_parts.append(f"+ {len(skipped_today)} deliberate-skip")
        one_liner = ", ".join(one_liner_parts) if one_liner_parts else "no proposals"

        pod = Pod(
            pod_id=pod_id,
            kind="intention.romantic_set",
            tags=["intention", "romantic_set"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by="romantic_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "proposal_pod_ids": proposal_pod_ids,
                "skipped_today": skipped_today,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[romantic_persist] mint intention.romantic_set failed: %s", e)
        return None
