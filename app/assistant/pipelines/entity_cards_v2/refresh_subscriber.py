"""Nightly card refresh — the live loop the v2 design specified but never wired
(docs/architecture/12_ENTITY_CARDS_V2_DESIGN.md step 5; audit 2026-06-10 found
zero callers and a roster frozen since May 22).

Selection is STATELESS: a card is stale iff its entity node — or any current
neighbor node — has updated_at newer than the card's last_built_at. Damping
(cards must not churn): a per-card COOLDOWN (default 7 days since last build;
changes accumulate and trigger ONE rebuild when it expires) plus a per-night
rebuild CAP (default 10, highest node importance first).

Also each run: builds cards for newly card-worthy entities (cap; built
inactive and activated only when the critic passes them — the L0 judge), and
deactivates cards whose entity node no longer exists (merge losers).

A failed build or critic call stops the run (owner, 2026-10-05): the
exception escapes to the routine runner, the rest stays pending for the next
run, and the shared failure ledger (kind "entity_card") sets aside an entity
whose card fails in three runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import text as sql_text

from app.assistant.kg import failure_ledger
from app.assistant.utils.logging_config import get_logger
from app.models.base import get_session

logger = get_logger(__name__)

DEFAULT_COOLDOWN_DAYS = 7
DEFAULT_MAX_REBUILDS = 10
DEFAULT_MAX_NEW = 5

LEDGER_KIND = "entity_card"

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


def _judge_new_card(entity_node_id: str, label: str) -> bool:
    """Run the L0 critic on a freshly built, still inactive card. pass/rewrite
    -> activate; veto -> the card stays inactive (its row keeps the entity out
    of the new-card queue). Returns True when vetoed. A failed or verdict-less
    critic call raises.

    Read -> close -> LLM -> short write transaction: the session must not
    ride the critic call (db_manager discipline, 2026-07-08 KG audit G6).
    """
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message
    from app.assistant.pipelines.entity_cards_v2.builder import _scope
    from app.models.db_manager import get_db_manager

    with get_db_manager().read_session() as session:
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
    data = getattr(resp, "data", None)
    verdict = data.get("verdict") if isinstance(data, dict) else None
    if verdict not in ("pass", "rewrite", "veto"):
        raise RuntimeError(f"card_critic returned no verdict for {label!r}: {data!r}")
    if verdict == "veto":
        logger.info("[card_refresh] critic vetoed new card %r: %s",
                    label, (data.get("reason") or "")[:140])
        return True
    with get_db_manager().transaction(op="card_refresh.critic_pass") as session:
        session.execute(sql_text(
            "UPDATE entity_card_v2 SET is_active = 1 WHERE entity_node_id = :nid"
        ), {"nid": entity_node_id})
    return False


def _discard_unjudged_card(entity_node_id: str) -> None:
    """A new card the critic did not judge is deleted, not left inactive: an
    inactive card reads as vetoed and its entity would never be offered again.
    With no card row the entity is a new-card candidate again next run, which
    rebuilds and judges it."""
    from app.assistant.entity_management.entity_card_v2 import EntityCardV2
    from app.models.db_manager import get_db_manager

    with get_db_manager().transaction(op="card_refresh.discard_unjudged") as session:
        card = session.query(EntityCardV2).filter(
            EntityCardV2.entity_node_id == entity_node_id,
            EntityCardV2.is_active.is_(False),
        ).first()
        if card is not None:
            session.delete(card)


def _build_new_card(entity_node_id: str, label: str) -> str:
    """Build one new card inactive, then judge it. Returns 'skipped', 'built'
    or 'vetoed'; raises on a failed build or critic call."""
    from app.assistant.pipelines.entity_cards_v2.builder import build_card

    res = build_card(entity_node_id, activate_new=False)
    if res.get("status") == "skipped":
        return "skipped"
    if res.get("status") != "ok":
        raise RuntimeError(f"new card build for {label!r} failed: {res}")
    try:
        vetoed = _judge_new_card(entity_node_id, label)
    except Exception:
        _discard_unjudged_card(entity_node_id)
        raise
    return "vetoed" if vetoed else "built"


def run_card_refresh(*, cooldown_days: int = DEFAULT_COOLDOWN_DAYS,
                     max_rebuilds: int = DEFAULT_MAX_REBUILDS,
                     max_new: int = DEFAULT_MAX_NEW,
                     dry_run: bool = False) -> Dict[str, Any]:
    from app.assistant.importance.consumers import is_card_worthy
    from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
    from app.assistant.pipelines.entity_cards_v2.builder import build_card

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=cooldown_days)).isoformat()
    set_aside = failure_ledger.set_aside_ids(LEDGER_KIND)

    session = get_session()
    try:
        from app.assistant.utils.identity_names import get_required_primary_user_name
        raw_stale = session.execute(
            sql_text(_STALE_SQL), {"cooldown_cutoff": cutoff, "cap": max_rebuilds + 5 + len(set_aside)}
        ).fetchall()
        # build_card always skips the user-self entity; its node updates daily,
        # so without this filter it would pin a rebuild slot every night.
        # Matched via the canonical name resolver — builder._is_user_self_entity
        # reads a scope-gated resource and silently returns False outside a
        # room context, which is exactly how the slot got wasted.
        user_name = get_required_primary_user_name().strip().lower()
        stale = [r for r in raw_stale
                 if (r[2] or "").strip().lower() != user_name
                 and r[1] not in set_aside][:max_rebuilds]
        orphans = [r[0] for r in session.execute(sql_text(_ORPHAN_SQL)).fetchall()]
        candidates = session.execute(sql_text(_NEW_CANDIDATE_SQL)).fetchall()

        new_worthy: List = []
        for nid, label in candidates:
            if len(new_worthy) >= max_new:
                break
            if nid in set_aside:
                continue
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
        "set_aside": len(set_aside),
        "rebuilt": 0, "built_new": 0, "vetoed_new": 0, "skipped": 0,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary

    # The first failed build stops the run: the exception escapes to the
    # routine runner and the remaining cards stay stale/new for the next run.
    for _card_id, nid, label, _imp in stale:
        try:
            res = build_card(nid)
            if res.get("status") not in ("ok", "skipped"):
                raise RuntimeError(f"card rebuild for {label!r} failed: {res}")
        except Exception as exc:
            failure_ledger.record_failure(LEDGER_KIND, nid, exc, label=label)
            raise
        failure_ledger.clear(LEDGER_KIND, nid)
        summary["rebuilt" if res["status"] == "ok" else "skipped"] += 1

    for nid, label in new_worthy:
        try:
            outcome = _build_new_card(nid, label)
        except Exception as exc:
            failure_ledger.record_failure(LEDGER_KIND, nid, exc, label=label)
            raise
        failure_ledger.clear(LEDGER_KIND, nid)
        if outcome == "skipped":
            summary["skipped"] += 1
            continue
        summary["built_new"] += 1
        if outcome == "vetoed":
            summary["vetoed_new"] += 1

    logger.info("[card_refresh] %s", summary)
    return summary
