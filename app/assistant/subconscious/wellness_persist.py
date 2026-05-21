"""Persist wellness_proposer output as intention pods.

v0 (mirrors meal_persist's intention.meal shape):
- Each WellnessProposal becomes one pod of kind=`intention.wellness`
- One summary pod of kind=`intention.wellness_set` per run (carries
  rest_day_advisory + skipped_today + free_form_thinking + refs)

No calendar writes from this layer yet — that's the "Post to Wellness
Calendar" button (deferred, same shape as the meal calendar sync).

The intention pods auto-surface in:
- noticer's exploration_outcomes_30d context (queries intention.* kinds)
- digest's wellness ideas section (once wired)
- recent_wellness_intentions context section above (next run sees prior)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_wellness_proposer_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Mint pods from a wellness_proposer run."""
    store = PodStore()
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    proposal_pod_ids: List[str] = []

    proposals = output.get("proposals") or []
    skipped_today = output.get("skipped_today") or []
    rest_day_advisory = output.get("rest_day_advisory")
    free_form_thinking = (output.get("free_form_thinking") or "").strip()

    for prop in proposals:
        pod_id = _mint_intention_wellness_pod(store, prop, now_utc_iso)
        if pod_id:
            proposal_pod_ids.append(pod_id)

    set_pod_id = _mint_intention_wellness_set_pod(
        store=store,
        proposal_pod_ids=proposal_pod_ids,
        rest_day_advisory=rest_day_advisory,
        free_form_thinking=free_form_thinking,
        skipped_today=skipped_today,
        now_utc_iso=now_utc_iso,
    )

    return {
        "proposal_pod_count": len(proposal_pod_ids),
        "proposal_pod_ids": proposal_pod_ids,
        "set_pod_id": set_pod_id,
        "rest_day_advisory_emitted": bool(rest_day_advisory),
        "skipped_count": len(skipped_today),
    }


def _mint_intention_wellness_pod(
    store: PodStore,
    proposal: Dict[str, Any],
    now_utc_iso: str,
) -> Optional[str]:
    """One pod per proposed wellness intention."""
    try:
        pod_id = f"datapod:intention.wellness:{uuid.uuid4().hex[:24]}"
        kind = proposal.get("kind") or "wellness"
        actors = proposal.get("actors") or []
        date = proposal.get("date") or ""
        summary = proposal.get("summary") or "(unnamed)"
        source = proposal.get("source") or "?"
        confidence = proposal.get("confidence") or "?"
        novelty = proposal.get("novelty") or "?"
        workout_type = proposal.get("workout_type")
        intensity = proposal.get("intensity")
        equipment = proposal.get("equipment_used") or []
        duration = proposal.get("duration_minutes")
        start_local = proposal.get("proposed_start_local")

        one_liner = f"{kind} {date}: {summary}"
        body_parts = [
            f"# Proposed wellness — {kind} on {date}",
            "",
            f"**Summary:** {summary}",
            f"**Actors:** {', '.join(actors) if actors else '(none specified)'}",
            f"**Source:** {source}  •  **Novelty:** {novelty}  •  **Confidence:** {confidence}",
        ]
        if start_local:
            body_parts.append(f"**Time:** {start_local}")
        if duration is not None:
            body_parts.append(f"**Duration:** {duration} min")
        if workout_type:
            body_parts.append(f"**Workout type:** {workout_type}")
        if intensity:
            body_parts.append(f"**Intensity:** {intensity}")
        if equipment:
            body_parts.append(f"**Equipment:** {', '.join(equipment)}")

        reasoning = (proposal.get("reasoning") or "").strip()
        if reasoning:
            body_parts += ["", "**Reasoning:**", reasoning]

        novelty_rationale = (proposal.get("novelty_rationale") or "").strip()
        if novelty_rationale:
            body_parts += ["", "**Why this novel pick:**", novelty_rationale]

        if addresses:
            body_parts += ["", "**Addresses concerns:** " + ", ".join(addresses)]

        body = "\n".join(body_parts)
        tags = ["intention", "wellness", kind]
        if novelty == "novel":
            tags.append("novel")
        if intensity:
            tags.append(f"intensity:{intensity}")

        pod = Pod(
            pod_id=pod_id,
            kind="intention.wellness",
            tags=tags,
            one_liner=one_liner,
            body=body,
            source_refs=[],  # concern_ids + intra-pod refs live in metadata
            for_agents=[],
            scope_id=None,
            created_by="wellness_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "kind": kind,
                "actors": actors,
                "date": date,
                "summary": summary,
                "source": source,
                "novelty": novelty,
                "confidence": confidence,
                "intensity": intensity,
                "workout_type": workout_type,
                "duration_minutes": duration,
                "equipment_used": equipment,
                "proposed_start_local": start_local,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[wellness_persist] mint intention.wellness failed: %s", e)
        return None


def _mint_intention_wellness_set_pod(
    *,
    store: PodStore,
    proposal_pod_ids: List[str],
    rest_day_advisory: Optional[str],
    free_form_thinking: str,
    skipped_today: List[str],
    now_utc_iso: str,
) -> Optional[str]:
    """Aggregate pod per wellness_proposer run — captures narrative +
    rest-day advisory + references to all per-intention pods."""
    try:
        pod_id = f"datapod:intention.wellness_set:{uuid.uuid4().hex[:24]}"

        body_parts = [
            f"# Wellness proposer set — {now_utc_iso}",
            "",
            f"**Proposals:** {len(proposal_pod_ids)}",
        ]
        if rest_day_advisory:
            body_parts += ["", "**Rest-day advisory:**", rest_day_advisory]
        if skipped_today:
            body_parts += ["", "**Skipped today:**"] + [f"- {s}" for s in skipped_today]
        if free_form_thinking:
            body_parts += ["", "**Free-form thinking:**", free_form_thinking]
        if proposal_pod_ids:
            body_parts += ["", "**Per-intention pods:**"] + [f"- {pid}" for pid in proposal_pod_ids]

        one_liner_parts = [f"{len(proposal_pod_ids)} wellness intentions"]
        if rest_day_advisory:
            one_liner_parts.append("+ rest-day")
        if skipped_today:
            one_liner_parts.append(f"+ {len(skipped_today)} skipped")
        one_liner = ", ".join(one_liner_parts)

        pod = Pod(
            pod_id=pod_id,
            kind="intention.wellness_set",
            tags=["intention", "wellness_set"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by="wellness_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "proposal_pod_ids": proposal_pod_ids,
                "rest_day_advisory": rest_day_advisory,
                "skipped_today": skipped_today,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[wellness_persist] mint intention.wellness_set failed: %s", e)
        return None
