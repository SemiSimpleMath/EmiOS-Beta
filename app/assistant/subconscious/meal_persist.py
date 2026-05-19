"""Persist meal_proposer output as intention pods.

v0 (Phase 1a) is propose-only:
- Each MealProposal becomes one pod of kind=`intention.meal`
- The ShoppingRun (if any) becomes one pod of kind=`intention.shopping`
- Fast food advisory + free_form_thinking ride in a single
  pod of kind=`intention.meal_set` so the noticer's digest can surface them

No calendar writes from this layer. That's Phase 1c.

The intention pods auto-surface in the noticer's `exploration_outcomes_30d`
context section (it queries pods with kind starting `intention.*`) AND in
the digest's "ideas inbox" section once we wire that.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def apply_meal_proposer_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Mint pods for each proposal + shopping_run + (optional) summary pod.

    Returns a summary dict for the runner to print.
    """
    store = PodStore()
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    proposal_pod_ids: List[str] = []
    shopping_pod_id: Optional[str] = None
    set_pod_id: Optional[str] = None

    proposals = output.get("proposals") or []
    shopping_run = output.get("shopping_run")
    fast_food_advisory = output.get("fast_food_advisory")
    free_form_thinking = (output.get("free_form_thinking") or "").strip()
    skipped_meals = output.get("skipped_meals") or []

    # 1. One pod per proposal
    for prop in proposals:
        pod_id = _mint_intention_meal_pod(store, prop, now_utc_iso)
        if pod_id:
            proposal_pod_ids.append(pod_id)

    # 2. Shopping run pod (if any)
    if isinstance(shopping_run, dict) and shopping_run.get("items"):
        shopping_pod_id = _mint_intention_shopping_pod(store, shopping_run, now_utc_iso)

    # 3. Summary pod — captures the proposer's narrative + advisory
    set_pod_id = _mint_intention_meal_set_pod(
        store=store,
        proposal_pod_ids=proposal_pod_ids,
        shopping_pod_id=shopping_pod_id,
        fast_food_advisory=fast_food_advisory,
        free_form_thinking=free_form_thinking,
        skipped_meals=skipped_meals,
        now_utc_iso=now_utc_iso,
    )

    return {
        "proposal_pod_count": len(proposal_pod_ids),
        "proposal_pod_ids": proposal_pod_ids,
        "shopping_pod_id": shopping_pod_id,
        "set_pod_id": set_pod_id,
        "fast_food_advisory_emitted": bool(fast_food_advisory),
        "skipped_meals_count": len(skipped_meals),
    }


def _mint_intention_meal_pod(
    store: PodStore,
    proposal: Dict[str, Any],
    now_utc_iso: str,
) -> Optional[str]:
    """One pod per proposed meal."""
    try:
        pod_id = f"datapod:intention.meal:{uuid.uuid4().hex[:24]}"
        actors = proposal.get("actors") or []
        meal_window = proposal.get("meal_window") or "meal"
        date = proposal.get("date") or ""
        dish = proposal.get("dish") or "(unnamed)"
        source = proposal.get("source") or "?"
        novelty = proposal.get("novelty") or "?"
        confidence = proposal.get("confidence") or "?"
        addresses = proposal.get("addresses_concern_ids") or []

        one_liner = f"{meal_window} {date} ({source}, {novelty}): {dish}"
        body_parts = [
            f"# Proposed meal — {meal_window} on {date}",
            "",
            f"**Dish:** {dish}",
            f"**Actors:** {', '.join(actors) if actors else '(none specified)'}",
            f"**Source:** {source}  •  **Novelty:** {novelty}  •  **Confidence:** {confidence}",
        ]
        if proposal.get("proposed_start_local"):
            body_parts.append(f"**Time:** {proposal['proposed_start_local']}")
        if proposal.get("estimated_calories_per_person"):
            body_parts.append(f"**Est. calories/person:** {proposal['estimated_calories_per_person']}")
        cost = proposal.get("estimated_total_cost_usd")
        if cost is not None:
            body_parts.append(f"**Est. total cost (USD):** {cost}")
        if proposal.get("recipe_ref"):
            body_parts.append(f"**Recipe:** {proposal['recipe_ref']}")

        ingredients = proposal.get("primary_ingredients") or []
        if ingredients:
            body_parts += ["", "**Ingredients:** " + ", ".join(ingredients)]
        needs = proposal.get("needs_shopping") or []
        if needs:
            body_parts.append("**Needs shopping:** " + ", ".join(needs))

        reasoning = (proposal.get("reasoning") or "").strip()
        if reasoning:
            body_parts += ["", "**Reasoning:**", reasoning]

        novelty_rationale = (proposal.get("novelty_rationale") or "").strip()
        if novelty_rationale:
            body_parts += ["", "**Why this novel pick:**", novelty_rationale]

        if addresses:
            body_parts += ["", "**Addresses concerns:** " + ", ".join(addresses)]

        body = "\n".join(body_parts)
        tags = ["intention", "meal_proposal", meal_window]
        if novelty == "novel":
            tags.append("novel")

        pod = Pod(
            pod_id=pod_id,
            kind="intention.meal",
            tags=tags,
            one_liner=one_liner,
            body=body,
            # PodSourceRef.kind is strict-Literal (unified_log /
            # event_repository:email / resource / image_file). Concern_ids
            # and intra-pod references don't fit. Keep those in metadata.
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by="meal_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "actors": actors,
                "meal_window": meal_window,
                "date": date,
                "dish": dish,
                "source": source,
                "novelty": novelty,
                "confidence": confidence,
                "addresses_concern_ids": addresses,
                "needs_shopping": needs,
                "primary_ingredients": ingredients,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[meal_persist] mint intention.meal failed: %s", e)
        return None


def _mint_intention_shopping_pod(
    store: PodStore,
    shopping_run: Dict[str, Any],
    now_utc_iso: str,
) -> Optional[str]:
    try:
        pod_id = f"datapod:intention.shopping:{uuid.uuid4().hex[:24]}"
        items = shopping_run.get("items") or []
        suggested = shopping_run.get("suggested_date") or ""
        reasoning = (shopping_run.get("reasoning") or "").strip()

        one_liner = f"Shopping run for {suggested}: {len(items)} items"
        body_parts = [
            f"# Proposed shopping run — {suggested}",
            "",
            "**Items:**",
        ] + [f"- {item}" for item in items]
        if reasoning:
            body_parts += ["", "**Reasoning:**", reasoning]

        pod = Pod(
            pod_id=pod_id,
            kind="intention.shopping",
            tags=["intention", "shopping"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by="meal_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "suggested_date": suggested,
                "items": items,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[meal_persist] mint intention.shopping failed: %s", e)
        return None


def _mint_intention_meal_set_pod(
    *,
    store: PodStore,
    proposal_pod_ids: List[str],
    shopping_pod_id: Optional[str],
    fast_food_advisory: Optional[str],
    free_form_thinking: str,
    skipped_meals: List[str],
    now_utc_iso: str,
) -> Optional[str]:
    """One pod per meal_proposer run — captures narrative + advisory +
    references to all the per-meal/shopping pods. The digest reads this."""
    try:
        pod_id = f"datapod:intention.meal_set:{uuid.uuid4().hex[:24]}"

        body_parts = [
            f"# Meal proposer set — {now_utc_iso}",
            "",
            f"**Proposals:** {len(proposal_pod_ids)}",
        ]
        if shopping_pod_id:
            body_parts.append(f"**Shopping run:** {shopping_pod_id}")
        else:
            body_parts.append("**Shopping run:** none")

        if skipped_meals:
            body_parts += ["", "**Skipped meals:**"] + [f"- {s}" for s in skipped_meals]

        if fast_food_advisory:
            body_parts += ["", "**Fast-food advisory:**", fast_food_advisory]

        if free_form_thinking:
            body_parts += ["", "**Free-form thinking:**", free_form_thinking]

        if proposal_pod_ids:
            body_parts += ["", "**Per-meal pods:**"] + [f"- {pid}" for pid in proposal_pod_ids]

        one_liner_parts = [f"{len(proposal_pod_ids)} meal proposals"]
        if shopping_pod_id:
            one_liner_parts.append("+ shopping")
        if fast_food_advisory:
            one_liner_parts.append("+ advisory")
        one_liner = ", ".join(one_liner_parts)

        pod = Pod(
            pod_id=pod_id,
            kind="intention.meal_set",
            tags=["intention", "meal_set"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[],  # see note on intention.meal — same constraint
            for_agents=[],
            scope_id=None,
            created_by="meal_proposer",
            metadata={
                "proposed_at_utc": now_utc_iso,
                "proposal_pod_ids": proposal_pod_ids,
                "shopping_pod_id": shopping_pod_id,
                "fast_food_advisory": fast_food_advisory,
                "skipped_meals": skipped_meals,
            },
        )
        store.put(pod)
        return pod_id
    except Exception as e:
        logger.warning("[meal_persist] mint intention.meal_set failed: %s", e)
        return None
