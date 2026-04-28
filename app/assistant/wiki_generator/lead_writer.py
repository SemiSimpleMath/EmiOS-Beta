"""Generate the Wikipedia-style lead for a wiki article.

Called after the per-section prose is stitched. Reads the article body,
hands it to the ``wiki_lead_writer`` agent, returns a 1–2 paragraph lead
that gets inserted between the H1 title and the first H2 section.

Error-safe: returns "" on any failure so a broken lead never blocks the
page from being written. The page just renders without a lead in that
case (and the consistency_critic will run unchanged).
"""
from __future__ import annotations

from typing import Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)

LEAD_AGENT = "wiki_lead_writer"


def _scope() -> ScopeContext:
    return ScopeContext(
        scope_id="scope::wiki::lead_writer",
        owner_id="jukka",
        actor_id="wiki_lead_writer",
        surface="ui",
        room_id="master_room",
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
    )


def generate_lead(
    *,
    entity_name: str,
    entity_type: str,
    article_body: str,
) -> str:
    """Return a 1–2 paragraph markdown lead, or "" on failure.

    ``article_body`` should be the full article markdown BELOW the H1 title
    (sections + see_also) — that is, the text the lead should faithfully
    summarize. Frontmatter and title should not be included.
    """
    body = (article_body or "").strip()
    if not body:
        return ""

    try:
        agent = DI.agent_factory.create_agent(LEAD_AGENT)
    except Exception as exc:
        logger.warning("[wiki_lead] agent_factory failed for %s: %s", LEAD_AGENT, exc)
        return ""
    if agent is None:
        logger.warning("[wiki_lead] agent_factory returned None for %s", LEAD_AGENT)
        return ""

    msg = Message(
        agent_input={
            "entity_name": entity_name,
            "entity_type": entity_type or "Entity",
            "article_body": body,
        },
        scope_context=_scope(),
    )

    try:
        result = agent.action_handler(msg)
    except Exception as exc:
        logger.warning("[wiki_lead] %s call failed for %r: %s", LEAD_AGENT, entity_name, exc)
        return ""

    data = getattr(result, "data", None) or {}
    lead = str(data.get("lead_markdown") or "").strip()
    if not lead:
        logger.warning("[wiki_lead] empty lead returned for %r", entity_name)
        return ""

    # Defensive: if the model accidentally emitted a heading or fence, strip it.
    lines = [ln for ln in lead.splitlines() if not ln.lstrip().startswith("```")]
    while lines and lines[0].lstrip().startswith("#"):
        lines.pop(0)
    return "\n".join(lines).strip()
