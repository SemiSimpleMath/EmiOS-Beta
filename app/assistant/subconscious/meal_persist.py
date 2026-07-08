"""Persist meal_proposer output as intention pods (two-axis: kind=intention
+ governed variant tag).

- Each MealProposal becomes one `intention` #meal pod
- The ShoppingRun (if any) becomes one `intention` #shopping pod
- Fast food advisory + free_form_thinking ride in the run-summary pod
  (`intention` #meal #run_summary) so the noticer's digest can surface them

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


def apply_daily_meal_proposer_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a daily_meal_proposer run: intention.meal pods per proposal
    + small ShoppingRun pod (if any) + meal_set summary pod."""
    return _apply_meal_proposer_output(output, agent_name="daily_meal_proposer")


# Legacy alias kept for any straggling callers; prefer the named function.
apply_meal_proposer_output = apply_daily_meal_proposer_output


def _apply_meal_proposer_output(output: Dict[str, Any], *, agent_name: str) -> Dict[str, Any]:
    """Shared persistence for the per-meal proposer (today, daily). Mints
    intention.meal pods + optional intention.shopping pod + summary set pod.

    NOTE: This does NOT handle weekly_shopping_list — that's part of the
    weekly_meal_planner pipeline; see apply_weekly_meal_planner_output.
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
        pod_id = _mint_intention_meal_pod(store, prop, now_utc_iso, agent_name=agent_name)
        if pod_id:
            proposal_pod_ids.append(pod_id)

    # 2. Shopping run pod (if any)
    if isinstance(shopping_run, dict) and shopping_run.get("items"):
        shopping_pod_id = _mint_intention_shopping_pod(
            store, shopping_run, now_utc_iso, agent_name=agent_name
        )

    # 3. Summary pod — captures the proposer's narrative + advisory
    set_pod_id = _mint_intention_meal_set_pod(
        store=store,
        proposal_pod_ids=proposal_pod_ids,
        shopping_pod_id=shopping_pod_id,
        fast_food_advisory=fast_food_advisory,
        free_form_thinking=free_form_thinking,
        skipped_meals=skipped_meals,
        now_utc_iso=now_utc_iso,
        agent_name=agent_name,
    )

    return {
        "proposal_pod_count": len(proposal_pod_ids),
        "proposal_pod_ids": proposal_pod_ids,
        "shopping_pod_id": shopping_pod_id,
        "set_pod_id": set_pod_id,
        "fast_food_advisory_emitted": bool(fast_food_advisory),
        "skipped_meals_count": len(skipped_meals),
    }


def apply_weekly_meal_planner_output(output: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a weekly_meal_planner run: mint a plan.weekly_meals pod
    (the strategic 7-day skeleton) + create/replace the agent's weekly
    shopping list Google Doc."""
    store = PodStore()
    now_utc_iso = datetime.now(timezone.utc).isoformat()

    weekly_plan = output.get("weekly_plan") or {}
    weekly_list = output.get("weekly_shopping_list") or {}
    free_form_thinking = (output.get("free_form_thinking") or "").strip()

    _coerce_invalid_slot_types(weekly_plan)

    plan_pod_id = _mint_weekly_plan_pod(
        store=store,
        weekly_plan=weekly_plan,
        free_form_thinking=free_form_thinking,
        now_utc_iso=now_utc_iso,
    )

    weekly_action = (weekly_list.get("action") or "none").lower()
    weekly_result = (
        _apply_weekly_shopping_list(weekly_list)
        if weekly_action in {"create", "replace"}
        else {"action": "none"}
    )

    return {
        "plan_pod_id": plan_pod_id,
        "slot_count": len((weekly_plan.get("slots") or [])),
        "weekly_list": weekly_result,
    }


def _coerce_invalid_slot_types(weekly_plan: Dict[str, Any]) -> None:
    """Safety net: per the schema, slot_type='flex'/'skip' are breakfast-
    only. If the model emits one on a lunch or dinner with a dish set,
    promote to 'home_cook' rather than letting an ambiguous slot through.
    If it emits flex/skip on a lunch/dinner with NO dish, log it but
    leave alone (rendering will show the gap so the user can spot it).
    """
    slots = weekly_plan.get("slots") or []
    promoted = 0
    for slot in slots:
        meal_window = (slot.get("meal_window") or "").lower()
        if meal_window not in {"lunch", "dinner"}:
            continue
        slot_type = (slot.get("slot_type") or "").lower()
        if slot_type not in {"flex", "skip"}:
            continue
        dish = (slot.get("dish") or "").strip()
        if dish:
            slot["slot_type"] = "home_cook"
            promoted += 1
        else:
            logger.warning(
                "[meal_persist] %s on %s tagged '%s' with no dish — "
                "leaving as-is; please correct the planner prompt",
                meal_window, slot.get("date", "?"), slot_type,
            )
    if promoted:
        logger.info(
            "[meal_persist] coerced %d lunch/dinner slot(s) from flex/skip → home_cook",
            promoted,
        )


def _apply_weekly_shopping_list(weekly_list: Dict[str, Any]) -> Dict[str, Any]:
    """Materialize the agent's weekly shopping list: create_google_doc on
    first run (bootstraps state), edit_google_doc thereafter. Updates the
    weekly_doc_state on success.
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.subconscious.meal_context_builder import (
        load_weekly_doc_state,
        save_weekly_doc_state,
    )
    from app.assistant.utils.pydantic_classes import ToolMessage

    action = (weekly_list.get("action") or "").lower()
    body = (weekly_list.get("body_markdown") or "").strip()
    week_start = weekly_list.get("week_start_date")
    if action not in {"create", "replace"} or not body:
        return {"action": "none", "reason": "empty_body_or_invalid_action"}

    state = load_weekly_doc_state()
    now_utc_iso = datetime.now(timezone.utc).isoformat()

    if action == "create":
        if state.get("doc_id"):
            logger.warning(
                "[meal_persist] weekly_list action=create but state already has doc_id=%s — "
                "treating as replace instead.", state["doc_id"],
            )
            action = "replace"

    if action == "create":
        title = f"Emi weekly shopping list (started {week_start or now_utc_iso[:10]})"
        try:
            cls = DI.tool_registry.get_tool_class("create_google_doc")
            if cls is None:
                return {"action": action, "status": "tool_unavailable", "tool": "create_google_doc"}
            tool = cls()
            tm = ToolMessage(
                tool_name="create_google_doc",
                tool_data={"arguments": {"title": title, "body": body}},
            )
            result = tool.execute(tm)
            data = getattr(result, "data", None) or {}
            doc_id = data.get("document_id") or data.get("doc_id")
            if not doc_id:
                return {"action": action, "status": "create_returned_no_doc_id", "raw": str(data)[:240]}
            state.update({
                "doc_id": doc_id,
                "doc_title": title,
                "last_built_week_start": week_start,
                "last_edit_at_utc": now_utc_iso,
            })
            save_weekly_doc_state(state)
            return {"action": "create", "status": "ok", "doc_id": doc_id, "title": title}
        except Exception as e:
            logger.exception("[meal_persist] create_google_doc failed")
            return {"action": action, "status": "exception", "error": str(e)[:240]}

    # action == "replace"
    # edit_google_doc is diff-based: find/replace. To overwrite the whole
    # body, we first read the current body, then diff-replace the entire
    # current body with the new body.
    doc_id = state.get("doc_id")
    if not doc_id:
        return {"action": action, "status": "no_doc_id_in_state"}
    try:
        # 1. Read current body
        get_cls = DI.tool_registry.get_tool_class("get_google_doc")
        if get_cls is None:
            return {"action": action, "status": "get_tool_unavailable"}
        get_tool = get_cls()
        get_tm = ToolMessage(
            tool_name="get_google_doc",
            tool_data={
                "arguments": {"document_id": doc_id, "include_body": True, "max_chars": 100000},
            },
        )
        get_result = get_tool.execute(get_tm)
        get_data = getattr(get_result, "data", None) or {}
        current_body = (get_data.get("body") or "").rstrip()
        if not current_body:
            # Empty doc — append the new body instead of diff (find="" is ambiguous)
            edit_cls = DI.tool_registry.get_tool_class("edit_google_doc")
            edit_tool = edit_cls()
            edit_tm = ToolMessage(
                tool_name="edit_google_doc",
                tool_data={
                    "arguments": {"document_id": doc_id, "operation": "append", "text": body},
                },
            )
            edit_result = edit_tool.execute(edit_tm)
            edit_data = getattr(edit_result, "data", None) or {}
            status = "ok_via_append" if not edit_data.get("error_code") else "edit_failed"
        else:
            # 2. Diff-replace current body with new body
            edit_cls = DI.tool_registry.get_tool_class("edit_google_doc")
            edit_tool = edit_cls()
            edit_tm = ToolMessage(
                tool_name="edit_google_doc",
                tool_data={
                    "arguments": {
                        "document_id": doc_id,
                        "operation": "diff",
                        "find": current_body,
                        "replace_with": body,
                    },
                },
            )
            edit_result = edit_tool.execute(edit_tm)
            edit_data = getattr(edit_result, "data", None) or {}
            status = "ok" if not edit_data.get("error_code") else "edit_failed"

        ok = status.startswith("ok")
        if ok:
            state.update({
                "last_built_week_start": week_start or state.get("last_built_week_start"),
                "last_edit_at_utc": now_utc_iso,
            })
            save_weekly_doc_state(state)
        return {
            "action": "replace",
            "status": status,
            "doc_id": doc_id,
            "edit_data_preview": str(edit_data)[:200],
        }
    except Exception as e:
        logger.exception("[meal_persist] edit_google_doc failed")
        return {"action": action, "status": "exception", "error": str(e)[:240]}


def _mint_intention_meal_pod(
    store: PodStore,
    proposal: Dict[str, Any],
    now_utc_iso: str,
    *,
    agent_name: str = "meal_proposer",
) -> Optional[str]:
    """One pod per proposed meal."""
    try:
        pod_id = f"datapod:intention:{uuid.uuid4().hex[:24]}"
        actors = proposal.get("actors") or []
        meal_window = proposal.get("meal_window") or "meal"
        date = proposal.get("date") or ""
        dish = proposal.get("dish") or "(unnamed)"
        source = proposal.get("source") or "?"
        novelty = proposal.get("novelty") or "?"
        confidence = proposal.get("confidence") or "?"

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

        body = "\n".join(body_parts)
        # Two-axis: kind=intention (handling class) + #meal (governed variant).
        tags = ["meal", meal_window]
        if novelty == "novel":
            tags.append("novel")

        pod = Pod(
            pod_id=pod_id,
            kind="intention",
            tags=tags,
            one_liner=one_liner,
            body=body,
            # PodSourceRef.kind is strict-Literal (unified_log /
            # event_repository:email / resource / image_file). Concern_ids
            # and intra-pod references don't fit. Keep those in metadata.
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by=agent_name,
            metadata={
                "proposed_at_utc": now_utc_iso,
                "actors": actors,
                "meal_window": meal_window,
                "date": date,
                "dish": dish,
                "source": source,
                "novelty": novelty,
                "confidence": confidence,
                "needs_shopping": needs,
                "primary_ingredients": ingredients,
            },
        )
        store.put(pod)
        return pod_id
    except Exception:
        # Fail loud — a swallowed warning here hid a NameError that silently dropped
        # EVERY meal pod in production (see scratch/MEAL-PLANNING-AUDIT.md).
        logger.exception("[meal_persist] mint intention.meal failed")
        raise


def _mint_intention_shopping_pod(
    store: PodStore,
    shopping_run: Dict[str, Any],
    now_utc_iso: str,
    *,
    agent_name: str,
) -> Optional[str]:
    try:
        pod_id = f"datapod:intention:{uuid.uuid4().hex[:24]}"
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
            kind="intention",
            tags=["shopping"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[],
            for_agents=[],
            scope_id=None,
            created_by=agent_name,
            metadata={
                "proposed_at_utc": now_utc_iso,
                "suggested_date": suggested,
                "items": items,
            },
        )
        store.put(pod)
        return pod_id
    except Exception:
        logger.exception("[meal_persist] mint intention.shopping failed")
        raise


def _mint_intention_meal_set_pod(
    *,
    store: PodStore,
    proposal_pod_ids: List[str],
    shopping_pod_id: Optional[str],
    fast_food_advisory: Optional[str],
    free_form_thinking: str,
    skipped_meals: List[str],
    now_utc_iso: str,
    agent_name: str = "meal_proposer",
) -> Optional[str]:
    """One pod per meal_proposer run — captures narrative + advisory +
    references to all the per-meal/shopping pods. The digest reads this."""
    try:
        pod_id = f"datapod:intention:{uuid.uuid4().hex[:24]}"

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
            # The run summary is the same handling class with a ROLE, not a
            # separate kind: #meal variant + #run_summary. Item consumers key
            # on metadata shape (date/dish), so it passes through them inert.
            kind="intention",
            tags=["meal", "run_summary"],
            one_liner=one_liner,
            body="\n".join(body_parts),
            source_refs=[
                PodSourceRef(kind="pod", id=pid)
                for pid in [*proposal_pod_ids, *( [shopping_pod_id] if shopping_pod_id else [] )]
            ],
            for_agents=[],
            scope_id=None,
            created_by=agent_name,
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
    except Exception:
        logger.exception("[meal_persist] mint meal run-summary pod failed")
        raise


def _mint_weekly_plan_pod(
    *,
    store: PodStore,
    weekly_plan: Dict[str, Any],
    free_form_thinking: str,
    now_utc_iso: str,
) -> Optional[str]:
    """Mint the plan.weekly_meals pod that the daily_meal_proposer reads.

    Body is a human-readable markdown rendering of the 7-day slot grid
    plus theme. Metadata carries the structured slot list for
    programmatic readers.
    """
    try:
        pod_id = f"datapod:plan:{uuid.uuid4().hex[:24]}"
        week_start = weekly_plan.get("week_start_date") or "?"
        slots = weekly_plan.get("slots") or []
        theme = (weekly_plan.get("week_theme") or "").strip()

        one_liner = f"Week of {week_start} — {len(slots)} slots"

        body_parts = [
            f"# Weekly meal plan — week of {week_start}",
            "",
            f"**Theme:** {theme}" if theme else "",
            "",
            "## Slots",
        ]
        # Group slots by date for readability
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        for slot in slots:
            d = slot.get("date") or "?"
            by_date.setdefault(d, []).append(slot)
        for date in sorted(by_date.keys()):
            dow = by_date[date][0].get("day_of_week", "?")
            body_parts.append(f"### {date} ({dow})")
            for slot in by_date[date]:
                window = slot.get("meal_window", "?")
                slot_type = slot.get("slot_type", "?")
                dish = slot.get("dish")
                leftover_from = slot.get("leftover_from_date")
                notes = (slot.get("notes") or "").strip()
                tail_parts = []
                if dish:
                    tail_parts.append(f"dish: {dish}")
                if leftover_from:
                    tail_parts.append(f"from: {leftover_from}")
                if notes:
                    tail_parts.append(notes)
                tail = " — " + " | ".join(tail_parts) if tail_parts else ""
                body_parts.append(f"- **{window}** [{slot_type}]{tail}")
            body_parts.append("")

        if free_form_thinking:
            body_parts += ["## Thinking", free_form_thinking]

        pod = Pod(
            pod_id=pod_id,
            kind="plan",
            tags=["weekly_meals"],
            one_liner=one_liner,
            body="\n".join([p for p in body_parts if p is not None]),
            source_refs=[],
            for_agents=["daily_meal_proposer"],
            scope_id=None,
            created_by="weekly_meal_planner",
            metadata={
                "produced_at_utc": now_utc_iso,
                "week_start_date": week_start,
                "slots": slots,
            },
        )
        store.put(pod)
        return pod_id
    except Exception:
        logger.exception("[meal_persist] mint plan.weekly_meals failed")
        raise
