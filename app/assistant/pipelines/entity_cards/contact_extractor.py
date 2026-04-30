"""Deterministic contact-info extraction from a KG neighborhood.

Covers the explicit contact-typed edges: ``email_address``, ``phone_number``,
``address``, ``login_email``, ``in_phone_contact``, etc. The value comes from
the counterpart node's label (or its ``original_sentence`` when that's all we
have).

This is NOT a full contact extractor. Most user contact data in the KG lives
inside ``has_state`` State nodes — e.g. ``Jukka has_state <State "Jukka's
work email is X">``. Extracting from those requires parsing the State node's
label or ``original_sentence``, which is the LLM writer's job.

So the pipeline is:
    1. This extractor pulls the obvious structured contacts.
    2. The LLM writer (later step) fills in anything else from bullets
       tagged "contact".

Keeps the LLM prompt smaller and stops the LLM from hallucinating values
when the KG already has them structurally.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from app.assistant.kg_projection import EntityNeighborhood

# Relationship types that encode contact info directly. Key: the raw edge
# type as stored; Value: the semantic type we want in the final card's
# contact_info entries.
CONTACT_RELATIONSHIP_TYPES: Dict[str, str] = {
    "email_address": "email",
    "login_email": "email",
    "phone_number": "phone",
    "in_phone_contact": "phone",
    "address": "address",
    "addresses": "address",
    "lives_at": "address",
    "has_link": "link",
    "has_handle": "handle",
    "uses_handle": "handle",
    "has_website": "link",
}


@dataclass
class ContactEntry:
    type: str              # semantic type: 'email' | 'phone' | 'address' | 'link' | 'handle' | ...
    value: str             # the actual contact value
    label: str = ""        # optional freeform label (e.g. 'work', 'personal')
    source_edge_id: str = ""
    source_node_id: str = ""


def extract_contact_info(neighborhood: EntityNeighborhood) -> List[ContactEntry]:
    """Return contact entries derivable from the neighborhood's misc edges.

    Walks ``misc_outgoing`` and ``misc_incoming`` looking for edges whose
    ``relationship_type`` is in ``CONTACT_RELATIONSHIP_TYPES``. The value is
    pulled from the counterpart node's label; falls back to
    ``original_sentence`` if the label is empty or looks like a placeholder.
    """
    out: List[ContactEntry] = []

    for edge, target in neighborhood.misc_outgoing:
        rel = (edge.relationship_type or "").strip()
        semantic_type = CONTACT_RELATIONSHIP_TYPES.get(rel)
        if not semantic_type:
            continue
        value = _extract_value(target)
        if not value:
            continue
        out.append(ContactEntry(
            type=semantic_type,
            value=value,
            label=rel.replace("_", " "),
            source_edge_id=str(edge.id) if getattr(edge, "id", None) else "",
            source_node_id=str(target.id) if getattr(target, "id", None) else "",
        ))

    for source, edge in neighborhood.misc_incoming:
        rel = (edge.relationship_type or "").strip()
        semantic_type = CONTACT_RELATIONSHIP_TYPES.get(rel)
        if not semantic_type:
            continue
        value = _extract_value(source)
        if not value:
            continue
        out.append(ContactEntry(
            type=semantic_type,
            value=value,
            label=rel.replace("_", " "),
            source_edge_id=str(edge.id) if getattr(edge, "id", None) else "",
            source_node_id=str(source.id) if getattr(source, "id", None) else "",
        ))

    # De-dup: same (type, value) from either direction → keep first.
    seen = set()
    unique: List[ContactEntry] = []
    for c in out:
        key = (c.type, c.value.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def _extract_value(node) -> str:
    """Pull the human-readable value string from a contact counterpart node."""
    label = (getattr(node, "label", "") or "").strip()
    if label and not _looks_like_placeholder(label):
        return label
    # Fall back to the original sentence — the raw fact from chat.
    sentence = (getattr(node, "original_sentence", "") or "").strip()
    return sentence


def _looks_like_placeholder(label: str) -> bool:
    """Heuristic: a contact value node with a generic label like 'email'
    or 'phone number' rather than the actual value. These are wiring errors
    in the KG and shouldn't be emitted as contact values."""
    lower = label.strip().lower()
    return lower in {"email", "phone", "phone number", "email address", "address", "link", "handle"}
