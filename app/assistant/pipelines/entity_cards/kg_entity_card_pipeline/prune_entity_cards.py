"""
Prune low-value entity cards (soft-delete by setting is_active=False).

This script is designed to help keep entity cards high-signal for prompt injection as the KG grows.

Default behavior is DRY RUN (no DB writes).
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List

from sqlalchemy import func

from app.models.base import get_session
from app.assistant.kg.db.knowledge_graph_db import Edge
from app.assistant.entity_management.entity_cards import EntityCard


def _looks_like_obvious_junk(name: str) -> bool:
    if not name:
        return True
    s = name.strip()
    if not s:
        return True
    low = s.lower()

    # Avoid the most egregious artifact class observed (inventory-like ammo calibers).
    if re.match(r"^\.\d+\b", s) and ("ammo" in low or "ammunition" in low):
        return True
    if ("ammunition" in low or " ammo" in low or low.endswith(" ammo")) and any(ch.isdigit() for ch in low):
        return True

    # Pure punctuation/numeric labels
    alnum = sum(1 for ch in s if ch.isalnum())
    if alnum <= 1:
        return True

    return False


def _is_multi_word(name: str) -> bool:
    """
    Multi-word labels are rarely useful as prompt-injection triggers because
    they only fire when the user types the exact phrase.  Single-word names
    ("Jukka", "Katy", "Finland") are far more likely to appear verbatim.

    Cards that have already proven their worth (usage_count > 0) are exempted
    from this check by the caller, so established multi-word cards survive.
    """
    return len(name.strip().split()) > 1


def _edge_degrees_for_nodes(session, node_ids: List[str]) -> Dict[str, int]:
    if not node_ids:
        return {}
    incoming = dict(
        session.query(Edge.target_id, func.count(Edge.id))
        .filter(Edge.target_id.in_(node_ids))
        .group_by(Edge.target_id)
        .all()
    )
    outgoing = dict(
        session.query(Edge.source_id, func.count(Edge.id))
        .filter(Edge.source_id.in_(node_ids))
        .group_by(Edge.source_id)
        .all()
    )
    degrees: Dict[str, int] = {}
    for nid in node_ids:
        degrees[nid] = int(incoming.get(nid, 0) or 0) + int(outgoing.get(nid, 0) or 0)
    return degrees


def prune_entity_cards(
    *,
    apply: bool,
    min_age_days: int,
    max_degree_keep: int,
    max_degree_prune: int,
    prune_concepts: bool = True,
    prune_multi_word_unused: bool = True,
) -> Dict:
    """
    Heuristic pruning:
    - Never prune high-degree cards (>= max_degree_keep).
    - Prefer pruning unused, low-degree, low-confidence, or obvious-junk-name cards.
    - prune_concepts: deactivate cards whose entity_type is "Concept" (unused only).
    - prune_multi_word_unused: deactivate cards whose name has >1 word and have never
      been used for injection (usage_count == 0).  Cards that have actually fired in
      prompts are left alone regardless of word count.

    This is intentionally conservative. Start with dry-run to inspect.
    """
    session = get_session()
    try:
        cards: List[EntityCard] = (
            session.query(EntityCard).filter(EntityCard.is_active == True).all()  # noqa: E712
        )
        node_ids = [c.source_node_id for c in cards if c.source_node_id]
        degrees = _edge_degrees_for_nodes(session, node_ids)

        now = datetime.now(timezone.utc)
        min_created_at = now - timedelta(days=min_age_days)
        min_created_at_naive = min_created_at.replace(tzinfo=None)

        decisions: List[Dict] = []
        to_deactivate: List[EntityCard] = []

        for c in cards:
            name = c.entity_name
            degree = degrees.get(c.source_node_id, 0) if c.source_node_id else 0
            unused = int(c.usage_count or 0) == 0 and c.last_used is None

            # Hard keep: highly connected nodes tend to have hard-fought summaries.
            if degree >= max_degree_keep:
                decisions.append(
                    {
                        "entity_name": name,
                        "action": "keep",
                        "reason": f"degree={degree} >= {max_degree_keep}",
                        "degree": degree,
                        "usage_count": int(c.usage_count or 0),
                        "confidence": c.confidence,
                    }
                )
                continue

            # SQLite often returns naive datetimes even if the column is declared timezone-aware.
            # Compare like-with-like.
            if c.created_at is None:
                age_ok = False
            elif getattr(c.created_at, "tzinfo", None) is None:
                age_ok = c.created_at < min_created_at_naive
            else:
                age_ok = c.created_at < min_created_at

            low_degree = degree <= max_degree_prune
            low_conf = c.confidence is not None and float(c.confidence) < 0.6
            junk_name = _looks_like_obvious_junk(name)
            is_concept = (c.entity_type or "").strip().lower() == "concept"
            multi_word = _is_multi_word(name)

            should_prune = False
            reasons = []

            if junk_name:
                should_prune = True
                reasons.append("obvious_junk_name")

            if prune_concepts and is_concept and unused and age_ok:
                should_prune = True
                reasons.append("concept+unused")

            if prune_multi_word_unused and multi_word and unused and age_ok:
                should_prune = True
                reasons.append("multi_word+unused")

            if unused and low_degree and age_ok:
                should_prune = True
                reasons.append(f"unused+low_degree(degree={degree})+age>={min_age_days}d")
            if unused and low_conf and low_degree and age_ok:
                should_prune = True
                reasons.append(f"unused+low_conf({c.confidence})+low_degree(degree={degree})+age>={min_age_days}d")

            if should_prune:
                decisions.append(
                    {
                        "entity_name": name,
                        "action": "deactivate" if apply else "would_deactivate",
                        "reason": ";".join(reasons) or "heuristic",
                        "degree": degree,
                        "usage_count": int(c.usage_count or 0),
                        "confidence": c.confidence,
                    }
                )
                if apply:
                    c.is_active = False
                    to_deactivate.append(c)
            else:
                decisions.append(
                    {
                        "entity_name": name,
                        "action": "keep",
                        "reason": "below pruning thresholds",
                        "degree": degree,
                        "usage_count": int(c.usage_count or 0),
                        "confidence": c.confidence,
                    }
                )

        if apply and to_deactivate:
            session.commit()

        return {
            "apply": apply,
            "total_active_cards": len(cards),
            "deactivated": len(to_deactivate),
            "decisions": decisions,
        }
    finally:
        session.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Prune low-value entity cards (soft delete).")
    p.add_argument("--apply", action="store_true", help="Actually deactivate cards (default: dry-run).")
    p.add_argument("--min-age-days", type=int, default=7, help="Only prune cards older than this.")
    p.add_argument("--max-degree-keep", type=int, default=4, help="Never prune cards with degree >= this.")
    p.add_argument("--max-degree-prune", type=int, default=2, help="Prefer pruning cards with degree <= this.")
    p.add_argument("--no-prune-concepts", action="store_true", help="Keep Concept-type cards (default: prune unused ones).")
    p.add_argument("--no-prune-multi-word", action="store_true", help="Keep unused multi-word cards (default: prune them).")
    args = p.parse_args()

    result = prune_entity_cards(
        apply=bool(args.apply),
        min_age_days=int(args.min_age_days),
        max_degree_keep=int(args.max_degree_keep),
        max_degree_prune=int(args.max_degree_prune),
        prune_concepts=not args.no_prune_concepts,
        prune_multi_word_unused=not args.no_prune_multi_word,
    )

    deacts = [d for d in result["decisions"] if d["action"] in {"would_deactivate", "deactivate"}]
    print(f"Active cards scanned: {result['total_active_cards']}")
    print(f"{'Deactivated' if args.apply else 'Would deactivate'}: {len(deacts)}")
    for d in deacts[:50]:
        print(
            f"- {d['entity_name']!r}: {d['action']} reason={d['reason']} degree={d['degree']} "
            f"usage={d['usage_count']} conf={d['confidence']}"
        )
    if len(deacts) > 50:
        print(f"... (+{len(deacts) - 50} more)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

