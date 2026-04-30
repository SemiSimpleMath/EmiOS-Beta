"""v2 entity-card writer.

Orchestrates:
    build_card_inputs(label) → CardInputs
         → entity_card_summarizer LLM → structured card dict
         → merge with deterministic contacts
         → (future) persist + return

The LLM call is a single atomic call per card — no accumulator, no
per-batch state. For very-large entities (Jukka-scale 2800+ bullets) this
won't fit in a single prompt; a sharded path can be added later. The
typical case — dozens of tagged bullets — fits comfortably.

Follows the DB-session discipline: no open session across the LLM call.
``build_card_inputs`` opens its own sessions internally and closes them
before returning. The writer only touches the DB for the final persist
(not yet implemented in this file).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.kg_projection import Bullet
from app.assistant.pipelines.entity_cards.card_inputs import (
    CardInputs,
    build_card_inputs,
)
from app.assistant.pipelines.entity_cards.contact_extractor import ContactEntry
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
)

logger = get_logger(__name__)

SUMMARIZER_AGENT = "entity_card_summarizer"

_SCOPE_SUMMARIZER = ScopeContext(
    scope_id="scope::entity_cards::card_writer",
    owner_id="jukka",
    actor_id="entity_card_summarizer",
    surface="ui",
    room_id="master_room",
    approval=ScopeApprovalPolicy(authority_level=100),
    resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
)


def generate_card(
    entity_label: str,
    *,
    tag_cache_root: Optional[Path] = None,
    scope_context: Optional[ScopeContext] = None,
) -> Dict[str, Any]:
    """Produce a complete v2 card dict for ``entity_label``.

    Flow:
      1. ``build_card_inputs`` — deterministic: neighborhood, bullets, tags,
         grouped slices, deterministic contacts.
      2. One LLM call to ``entity_card_summarizer``, given the tagged
         bullets + known contacts, produces
         ``{summary, key_facts, contact_info, relevance_reason}``.
      3. Merge the LLM's contact_info with the deterministic contacts
         (dedup by (type, value)).

    Returns a dict suitable for persistence (persistence not in this
    function — caller decides). Dict shape:
        {
          "entity_name": ...,
          "entity_type": ...,
          "source_node_id": ...,
          "summary": ...,
          "key_facts": [...],
          "contact_info": [{"type", "value", "label"}, ...],
          "aliases": [...],
          "relevance_reason": ...,
          "_inputs": CardInputs,   # for inspection/debug; caller strips
        }
    """
    inputs = build_card_inputs(
        entity_label,
        tag_cache_root=tag_cache_root,
        scope_context=scope_context,
    )

    if inputs.bullet_count == 0:
        logger.info(
            "Card for %s: no bullets → minimal card (aliases + deterministic contacts only).",
            entity_label,
        )
        return _minimal_card(inputs)

    scope = scope_context or _SCOPE_SUMMARIZER
    agent = DI.agent_factory.create_agent(SUMMARIZER_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {SUMMARIZER_AGENT!r}")

    tagged_block = _format_tagged_bullets_block(inputs.grouped_bullets, inputs.taxonomy)
    known_contacts_block = _format_known_contacts_block(inputs.contact_info)

    msg = Message(
        agent_input={
            "entity_name": inputs.entity_name,
            "entity_type": inputs.entity_type,
            "entity_description": inputs.description,
            "entity_aliases": ", ".join(inputs.aliases) if inputs.aliases else "",
            "tagged_bullets": tagged_block,
            "known_contacts": known_contacts_block,
        },
        scope_context=scope,
    )
    response = agent.action_handler(msg)
    data = getattr(response, "data", None) or {}

    summary = str(data.get("summary") or "").strip()
    key_facts = _coerce_list_of_str(data.get("key_facts"))
    llm_contacts = _coerce_contact_list(data.get("contact_info"))
    relevance_reason = data.get("relevance_reason")
    if isinstance(relevance_reason, str):
        relevance_reason = relevance_reason.strip() or None

    merged_contacts = _merge_contacts(inputs.contact_info, llm_contacts)

    return {
        "entity_name": inputs.entity_name,
        "entity_type": inputs.entity_type,
        "source_node_id": inputs.entity_node_id,
        "summary": summary,
        "key_facts": key_facts,
        "contact_info": merged_contacts,
        "aliases": inputs.aliases,
        "relevance_reason": relevance_reason,
        "_inputs": inputs,
    }


# ---------- helpers ----------


def _minimal_card(inputs: CardInputs) -> Dict[str, Any]:
    """Card shape for entities with no bullets — passes through deterministic data only."""
    return {
        "entity_name": inputs.entity_name,
        "entity_type": inputs.entity_type,
        "source_node_id": inputs.entity_node_id,
        "summary": inputs.description or f"{inputs.entity_name} ({inputs.entity_type}).",
        "key_facts": [],
        "contact_info": [
            {"type": c.type, "value": c.value, "label": c.label}
            for c in inputs.contact_info
        ],
        "aliases": inputs.aliases,
        "relevance_reason": None,
        "_inputs": inputs,
    }


def _format_tagged_bullets_block(
    grouped: Dict[str, List[Bullet]],
    taxonomy,
) -> str:
    """Render the grouped-by-section bullets as a markdown block the LLM can read.

    Sections appear in taxonomy order, skipped if empty. Unknown-tag bullets
    (shouldn't happen with a well-formed taxonomy) land under an "Other"
    heading at the end.
    """
    sections_in_order: List[str] = []
    if taxonomy:
        for s in taxonomy:
            if s.key in grouped and grouped[s.key]:
                sections_in_order.append(s.key)
        known = {s.key for s in taxonomy}
        for key in grouped.keys():
            if key not in known:
                sections_in_order.append(key)
    else:
        sections_in_order = [k for k, v in grouped.items() if v]

    if not sections_in_order:
        return "(no tagged bullets)"

    key_to_title = {s.key: s.title for s in taxonomy} if taxonomy else {}

    out: List[str] = []
    for key in sections_in_order:
        title = key_to_title.get(key, key.replace("_", " ").title())
        out.append(f"## {title}")
        out.append("")
        for b in grouped[key]:
            out.append(b.text)
        out.append("")

    return "\n".join(out).strip()


def _format_known_contacts_block(contacts: List[ContactEntry]) -> str:
    if not contacts:
        return ""
    lines = []
    for c in contacts:
        label = f" ({c.label})" if c.label else ""
        lines.append(f"- **{c.type}**{label}: {c.value}")
    return "\n".join(lines)


def _coerce_list_of_str(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for item in raw:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
        elif isinstance(item, dict):
            # key_facts sometimes arrives as {fact: "..."} or {text: "..."}
            s = str(item.get("fact") or item.get("text") or item.get("value") or "").strip()
            if s:
                out.append(s)
    return out


def _coerce_contact_list(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").strip().lower()
        v = str(item.get("value") or "").strip()
        if not t or not v:
            continue
        out.append({
            "type": t,
            "value": v,
            "label": str(item.get("label") or "").strip(),
        })
    return out


def _merge_contacts(
    deterministic: List[ContactEntry],
    llm: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """Merge deterministic + LLM-extracted contacts, dedupped by (type, value)."""
    seen = set()
    out: List[Dict[str, str]] = []
    for c in deterministic:
        key = (c.type.lower(), c.value.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"type": c.type, "value": c.value, "label": c.label})
    for c in llm:
        key = (c["type"].lower(), c["value"].strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


# ---------- persistence ----------


def generate_and_persist_card(
    entity_label: str,
    *,
    force: bool = True,
    tag_cache_root: Optional[Path] = None,
    scope_context: Optional[ScopeContext] = None,
    blocking: bool = True,
) -> Dict[str, Any]:
    """Generate a v2 card and write it to the ``entity_cards`` table.

    DB session is only opened for the short write phase — the LLM call in
    ``generate_card`` happens with no session held (see
    ``feedback_no_db_lock_over_llm``).

    Card generation across the whole process is serialized through a
    shared lock (`card_gen_slot`), so concurrent callers from any path —
    admin UI, maintenance regen, nightly bulk runner, future writers —
    queue up rather than corrupt the entity_cards / entity_card_index
    pair. Pass ``blocking=False`` to raise ``CardGenSlotBusy`` immediately
    on contention (used by the admin UI's "busy → 409" path).

    Returns the card dict (without the ``_inputs`` debug field stripped).
    Raises if the entity has no KG node (nothing to source from).
    """
    from app.assistant.pipelines.entity_cards.card_gen_lock import card_gen_slot

    with card_gen_slot(blocking=blocking):
        return _generate_and_persist_card_unlocked(
            entity_label,
            force=force,
            tag_cache_root=tag_cache_root,
            scope_context=scope_context,
        )


def _generate_and_persist_card_unlocked(
    entity_label: str,
    *,
    force: bool,
    tag_cache_root: Optional[Path],
    scope_context: Optional[ScopeContext],
) -> Dict[str, Any]:
    """Body of generate_and_persist_card. Caller MUST hold the card_gen_slot."""
    card = generate_card(
        entity_label,
        tag_cache_root=tag_cache_root,
        scope_context=scope_context,
    )

    inputs: CardInputs = card["_inputs"]

    # Build the node_info dict shape the v1 persistence helper expects.
    entity = inputs.neighborhood.entity if inputs.neighborhood else None
    node_info: Dict[str, Any] = {
        "label": inputs.entity_name,
        "type": inputs.entity_type,
        "description": inputs.description,
        "aliases": inputs.aliases,
    }

    # Map the v2 card dict onto the kwargs store_entity_card_in_db expects.
    entity_card_payload = {
        "entity_type": inputs.entity_type,
        "summary": card["summary"],
        "key_facts": card["key_facts"],
        "relationships": [],   # v2 doesn't produce a separate relationships field — embedded in key_facts instead.
        "contact_info": card["contact_info"],
        "aliases": inputs.aliases,
        "user_relevance_reason": card.get("relevance_reason"),
        "confidence": None,
    }

    from app.assistant.pipelines.entity_cards.kg_entity_card_pipeline.kg_entity_card_pipeline import (
        store_entity_card_in_db,
    )
    from app.models.base import get_session

    unique_connected = 0
    if inputs.neighborhood is not None:
        unique_connected = (
            len(inputs.neighborhood.relationships)
            + len(inputs.neighborhood.events)
            + len(inputs.neighborhood.beliefs_about)
            + len(inputs.neighborhood.goals_targeting)
            + len(inputs.neighborhood.misc_outgoing)
            + len(inputs.neighborhood.misc_incoming)
        )

    session = get_session()
    try:
        store_entity_card_in_db(
            session,
            inputs.entity_name,
            entity_card_payload,
            source_node_id=inputs.entity_node_id,
            node_info=node_info,
            unique_connected_nodes=unique_connected,
            commit=True,
            force=force,
        )
    finally:
        session.close()

    logger.info(
        "v2 card persisted: %s (%s) — %d key_facts, %d contacts",
        inputs.entity_name, inputs.entity_type,
        len(card["key_facts"]), len(card["contact_info"]),
    )
    return card
