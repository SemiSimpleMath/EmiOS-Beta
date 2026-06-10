"""Nightly card refresh — the live loop the v2 design specified but never wired
(docs/architecture/12_ENTITY_CARDS_V2_DESIGN.md step 5; audit 2026-06-10 found
zero callers and a roster frozen since May 22).

Selection is STATELESS: a card is stale iff its entity node — or any current
neighbor node — has updated_at newer than the card's last_built_at. Damping
(cards must not churn): a per-card COOLDOWN (default 7 days since last build;
changes accumulate and trigger ONE rebuild when it expires) plus a per-night
rebuild CAP (default 10, highest node importance first).

Also each run: builds cards for newly card-worthy entities (cap, critic-vetoed
when the rendered content is noise — the L0 judge), and deactivates cards
whose entity node no longer exists (merge losers).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import text as sql_text

from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

DEFAULT_COOLDOWN_DAYS = 7
DEFAULT_MAX_REBUILDS = 10
DEFAULT_MAX_NEW = 5

_STALE_SQL = """
SELECT c.id card_id, c.entity_node_id, n.label, COALESCE(n.importance, 0) imp
FROM entity_card_v2 c
JOIN kg_node_metadata n ON n.id = c.entity_node_id
WHERE c.is_active = 1
  AND c.last_built_at < :cooldown_cutoff
  AND (
    n.updated_at > c.last_built_at
    OR EXISTS (
      SELECT 1 FROM kg_edge_metadata e
      JOIN kg_node_metadata m
        ON m.id = CASE WHEN e.source_id = c.entity_node_id
                       THEN e.target_id ELSE e.source_id END
      WHERE (e.source_id = c.entity_node_id OR e.target_id = c.entity_node_id)
        AND m.updated_at > c.last_built_at
    )
  )
ORDER BY imp DESC
LIMIT :cap
"""

_ORPHAN_SQL = """
SELECT c.id FROM entity_card_v2 c
WHERE c.is_active = 1
  AND NOT EXISTS (SELECT 1 FROM kg_node_metadata n WHERE n.id = c.entity_node_id)
"""

_NEW_CANDIDATE_SQL = """
SELECT n.id, n.label FROM kg_node_metadata n
WHERE n.node_type = 'Entity'
  AND NOT EXISTS (SELECT 1 FROM entity_card_v2 c WHERE c.entity_node_id = n.id)
ORDER BY COALESCE(n.importance, 0) DESC
LIMIT 200
"""


def _critic_veto_if_noise(entity_node_id: str, label: str) -> bool:
    """Run the L0 critic on a freshly built card; veto -> deactivate. Returns
    True when the card was vetoed. Critic errors keep the card (logged)."""
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    from app.assistant.pipelines.entity_cards_v2.builder import _scope

    session = get_session()
    try:
        rows = session.execute(sql_text(
            "SELECT s.section_name, s.intro_text FROM entity_card_section s "
            "JOIN entity_card_v2 c ON s.card_id = c.id "
            "WHERE c.entity_node_id = :nid ORDER BY s.position"
        ), {"nid": entity_node_id}).fetchall()
        brief = [f"ENTITY: {label}", "", "CARD AS RENDERED:"]
        brief += [f"  [{r[0]}] {(r[1] or '')[:400]}" for r in rows if (r[1] or "").strip()]
        brief += ["", "EVIDENCE (highest-importance graph facts about this entity):",
                  "  (the card above was just rendered from the entity's full graph)"]
        agent = DI.agent_factory.create_agent("entity_cards::card_critic")
        if agent is None:
            raise RuntimeError("card_critic agent unavailable")
        resp = agent.action_handler(Message(
            agent_input={"card_brief": "\n".join(brief)}, scope_context=_scope()))
        data = resp.data if (resp is not None and getattr(resp, "data", None)) else {}
        if data.get("verdict") == "veto":
            session.execute(sql_text(
                "UPDATE entity_card_v2 SET is_active = 0 WHERE entity_node_id = :nid"
            ), {"nid": entity_node_id})
            session.commit()
            logger.info("[card_refresh] critic vetoed new card %r: %s",
                        label, (data.get("reason") or "")[:140])
            return True
        return False
    except Exception as e:
        logger.error("[card_refresh] critic QA failed for %r (card kept): %s", label, e)
        return False
    finally:
        session.close()


def run_card_refresh(*, cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
                     max_rebuilds: int = DEFAULT_MAX_REBUILDS,
                     max_new: int = DEFAULT_MAX_NEW,
                     dry_run: bool = False) -> Dict[str, Any]:
    from app.assistant.importance.consumers import is_card_worthy
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.pipelines.entity_cards_v2.builder import build_card

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=cooldown_days)).isoformat()

    session = get_session()
    try:
        from app.assistant.pipelines.entity_cards_v2.builder import _is_user_self_entity
        raw_stale = session.execute(
            sql_text(_STALE_SQL), {"cooldown_cutoff": cutoff, "cap": max_rebuilds + 5}
        ).fetchall()
        # build_card always skips the user-self entity; its node updates daily,
        # so without this filter it would pin a rebuild slot every night.
        stale = [r for r in raw_stale
                 if not _is_user_self_entity(session.get(Node, r[1]))][:max_rebuilds]
        orphans = [r[0] for r in session.execute(sql_text(_ORPHAN_SQL)).fetchall()]
        candidates = session.execute(sql_text(_NEW_CANDIDATE_SQL)).fetchall()

        new_worthy: List = []
        for nid, label in candidates:
            if len(new_worthy) >= max_new:
                break
            node = session.get(Node, nid)
            if node is not None and is_card_worthy(node):
                new_worthy.append((nid, label))

        if orphans and not dry_run:
            for cid in orphans:
                session.execute(sql_text(
                    "UPDATE entity_card_v2 SET is_active = 0 WHERE id = :cid"), {"cid": cid})
            session.commit()
    finally:
        session.close()

    summary: Dict[str, Any] = {
        "stale_selected": [r[2] for r in stale],
        "new_selected": [label for _, label in new_worthy],
        "orphans_deactivated": len(orphans),
        "rebuilt": 0, "built_new": 0, "vetoed_new": 0, "failed": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    for r in stale:
        try:
            res = build_card(r[1])
            if res.get("status") in ("ok", "built", "success"):
                summary["rebuilt"] += 1
            else:
                summary["failed"] += 1
                logger.warning("[card_refresh] rebuild %r -> %s", r[2], res)
        except Exception as e:
            summary["failed"] += 1
            logger.error("[card_refresh] rebuild %r crashed: %s", r[2], e)

    for nid, label in new_worthy:
        try:
            res = build_card(nid)
            if res.get("status") in ("ok", "built", "success"):
                summary["built_new"] += 1
                if _critic_veto_if_noise(nid, label):
                    summary["vetoed_new"] += 1
            else:
                summary["failed"] += 1
        except Exception as e:
            summary["failed"] += 1
            logger.error("[card_refresh] new build %r crashed: %s", label, e)

    logger.info("[card_refresh] %s", summary)
    return summary
