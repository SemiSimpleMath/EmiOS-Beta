"""
reasoning_agent.py — Single emi_team_manager agent for situation research.

Stage 3 of the context_engine pipeline.

Receives a SituationBrief, asks the agent to:
    1. Generate questions/concerns about the situation.
    2. Classify each: researchable via KG vs needs user input.
    3. Call ask_kg for the KG-researchable ones (max 3 calls).
    4. Produce structured output: answered, open_for_user, suggested_actions.

After completion, optionally emits a proactive chat message to the user
with a concise summary of key open questions and suggested actions.
"""

from __future__ import annotations

import time

from app.assistant.utils.logging_config import get_logger
from app.assistant.context_engine.situation_brief import SituationBrief

logger = get_logger(__name__)

_REASONING_TASK_TEMPLATE = """\
The user said: "{user_message}"

You have been given a pre-computed situation dossier about {subject} \
and their connection to {primary_user}. Your job is to think through what needs to \
happen for this situation.

{dossier}

Using the dossier:
1. Generate a comprehensive list of questions and concerns (logistics, social, \
practical, emotional — be thorough).
2. For each item, decide: can this be researched via ask_kg, or does it require \
human input?
3. For items researchable via ask_kg — call ask_kg now (max 3 calls, keep questions \
narrow and specific).
4. Write your final answer as a structured markdown summary with sections:
   - **Answered** — questions you resolved from the KG
   - **Open for {primary_user}** — questions/concerns that need human input
   - **Suggested Actions** — concrete things that can or should be done

Do not re-derive the dossier — it is already complete. Focus on what comes next.
"""


def run_reasoning_agent(
    brief: SituationBrief,
    *,
    primary_user: str,
    owner_id: str,
) -> str:
    """
    Run the reasoning agent over a SituationBrief.

    Args:
        brief:        The situation brief from context_activation.
        primary_user: Primary user label (for task framing).
        owner_id:     KG owner / room_id (for scope).

    Returns:
        The agent's plain-text response (content field).

    Raises:
        Any exception from the agent framework propagates — no silent fallback.
    """
    t0 = time.perf_counter()

    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import (
        Message, ScopeApprovalPolicy, ScopeContext, ScopeToolPolicy,
    )

    visitor_seeds = [s for s in brief.seeds if s.lower() != primary_user.lower()]
    subject = visitor_seeds[0] if visitor_seeds else brief.seeds[0]

    task = _REASONING_TASK_TEMPLATE.format(
        user_message=brief.user_message,
        subject=subject,
        primary_user=primary_user,
        dossier=brief.situation_brief,
    )

    scope = ScopeContext(
        scope_id=f"scope::context_engine::{owner_id}::{brief.brief_id}",
        owner_id=owner_id,
        actor_id="context_engine_reasoning",
        surface="internal",
        # Ceiling = only the tool this agent needs (ask_kg). Authority 99 is the
        # autonomous-subsystem default (routines run at 99); ask_kg reads the KG
        # at the standard personal-data floor (90), so 99 clears it. The scope
        # can do nothing but ask_kg — minimal authority surface despite the 99.
        tools=ScopeToolPolicy(allowed_tools=["ask_kg"]),
        approval=ScopeApprovalPolicy(authority_level=99),
    )

    manager = DI.multi_agent_manager_factory.create_manager("emi_team_manager")

    result = manager.request_handler(
        Message(
            task=task,
            information=brief.situation_brief,
            content=task,
            scope_context=scope,
            data={
                "visible_tools": ["ask_kg"],
                "task_llm_params": {
                    "llm_provider": "openai",
                    "engine": "gpt-5.6-luna",
                },
            },
        )
    )

    elapsed = time.perf_counter() - t0
    logger.debug(
        "reasoning_agent: completed brief_id=%s in %.1fs",
        brief.brief_id,
        elapsed,
    )

    output_text: str = getattr(result, "content", None) or str(result)
    return output_text


# ---------------------------------------------------------------------------
# No proactive emit here — context_memo.py handles distillation + blackboard
# write after the pipeline completes. ConversationStarterStage fires the
# first question when the room goes quiet.
# ---------------------------------------------------------------------------
