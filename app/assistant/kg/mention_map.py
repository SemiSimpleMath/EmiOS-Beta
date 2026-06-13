"""Durable mention resolutions (identity phase 5).

lookup_mention: closed-form resolution for forms the node_merger has
already confirmed — validated on every hit (target alive? form still
unambiguous?) and self-revoking when the world changed.

mint_mention: records a confirmed bind, but ONLY when the form is
unambiguous graph-wide. The unambiguity test is data-derived (the
no-hardcoded-domain-knowledge doctrine): a form is ambiguous when any
OTHER same-type node claims it as label or alias, or a Disambiguation
marker exists at it.

Forms that match some node's exact label are never minted or consulted —
the exact-label tier already resolves those for free, earlier in the
ladder.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from sqlalchemy import func

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.time_utils import utc_now

logger = get_logger(__name__)


def normalize_mention(label: str) -> str:
    return " ".join((label or "").lower().strip().split())


def _competing_claimants(session, mention_norm: str, node_type: str,
                         allowed_node_id: Optional[str]) -> Optional[str]:
    """Why the form is ambiguous, or None when it is clean.

    Claimants: a Disambiguation marker at the form, or any same-type node
    OTHER than allowed_node_id whose label or alias equals the form.
    """
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.kg.disambiguation import find_disambiguation

    if find_disambiguation(session, mention_norm) is not None:
        return "Disambiguation marker exists at this form"

    label_hit = (
        session.query(Node.id)
        .filter(func.lower(Node.label) == mention_norm)
        .filter(Node.node_type == node_type)
        .filter(Node.id != (allowed_node_id or ""))
        .first()
    )
    if label_hit:
        return f"another {node_type} carries this form as its label"

    like_pat = f'%"{mention_norm}"%'
    alias_hit = (
        session.query(Node.id)
        .filter(Node.node_type == node_type)
        .filter(Node.id != (allowed_node_id or ""))
        .filter(func.lower(func.coalesce(func.cast(Node.aliases, type_=__import__("sqlalchemy").String), "")).like(like_pat))
        .first()
    )
    if alias_hit:
        return f"another {node_type} carries this form as an alias"
    return None


def lookup_mention(session, label: str, node_type: str) -> Optional[Any]:
    """Return the confirmed referent Node for this mention form, or None.

    Validates on every hit; a stale entry (target gone, form contested)
    is revoked in place and the lookup misses — the caller's ladder
    continues to the confirm tier.
    """
    from app.assistant.database.kg_mention_map import KGMentionMap
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    norm = normalize_mention(label)
    if not norm:
        return None

    entry = (
        session.query(KGMentionMap)
        .filter(KGMentionMap.mention_norm == norm)
        .filter(KGMentionMap.node_type == node_type)
        .filter(KGMentionMap.revoked_at.is_(None))
        .first()
    )
    if entry is None:
        return None

    node = session.get(Node, entry.node_id)
    if node is None:
        entry.revoked_at = utc_now()
        entry.revoked_reason = "target node no longer exists (merged or deleted)"
        logger.info("[mention_map] revoked %r — target gone", norm)
        return None

    contested = _competing_claimants(session, norm, node_type, entry.node_id)
    if contested:
        entry.revoked_at = utc_now()
        entry.revoked_reason = f"form became ambiguous: {contested}"
        logger.info("[mention_map] revoked %r — %s", norm, contested)
        return None

    entry.use_count = (entry.use_count or 0) + 1
    entry.last_used_at = utc_now()
    return node


def mint_mention(
    session, *, label: str, node_type: str, node_id: str,
    minted_by: str, source_proposal_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """Record a confirmed bind. Returns (minted, reason).

    Refuses (sink, not error) when: the form equals the target's own
    label (exact tier covers it), the form is contested graph-wide, or a
    live entry already exists (same target → no-op; different target →
    the form just proved ambiguous, so the EXISTING entry is revoked).
    """
    from app.assistant.database.kg_mention_map import KGMentionMap
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node

    norm = normalize_mention(label)
    if not norm:
        return False, "empty form"

    node = session.get(Node, node_id)
    if node is None:
        return False, "target node missing"
    if normalize_mention(node.label or "") == norm:
        return False, "form equals the node's own label (exact tier owns it)"

    existing = (
        session.query(KGMentionMap)
        .filter(KGMentionMap.mention_norm == norm)
        .filter(KGMentionMap.node_type == node_type)
        .filter(KGMentionMap.revoked_at.is_(None))
        .first()
    )
    if existing is not None:
        if existing.node_id == node_id:
            return False, "already mapped to this node"
        existing.revoked_at = utc_now()
        existing.revoked_reason = (
            f"confirmed bind to a DIFFERENT node ({node_id[:8]}) proved the "
            f"form ambiguous"
        )
        logger.info("[mention_map] revoked %r — conflicting confirmed bind", norm)
        return False, "form proved ambiguous (existing entry revoked)"

    contested = _competing_claimants(session, norm, node_type, node_id)
    if contested:
        return False, f"form contested: {contested}"

    session.add(KGMentionMap(
        mention_norm=norm,
        node_type=node_type,
        node_id=node_id,
        minted_by=minted_by,
        source_proposal_id=source_proposal_id,
    ))
    logger.info("[mention_map] minted %r -> %s (%s)", norm, node_id[:8], minted_by)
    return True, "minted"
