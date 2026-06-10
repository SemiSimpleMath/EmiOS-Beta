"""Run the wiki_consistency_critic agent on a generated prose page and file
any contradictions into the kg_maintenance_finding table.

The critic reads ONLY the rendered prose — it cannot see the graph. That's
intentional: the wiki is a QA surface, and internal contradictions there
often signal real KG issues (tense errors, duplicate nodes, classifier
mistakes) that were invisible at the graph level.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import frontmatter

from sqlalchemy import text as sql_text

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.database.kg_maintenance_finding import KGMaintenanceFinding
from app.assistant.utils.filename_safety import safe_filename
from app.assistant.utils.logging_config import get_logger
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.identity_names import PRINCIPAL_USER
from app.assistant.utils.pydantic_classes import Message, ScopeContext
from app.models.base import get_session

logger = get_logger(__name__)

CRITIC_AGENT = "wiki_consistency_critic"
FINDING_TYPE = "wiki_contradiction"


def _scope() -> ScopeContext:
    return load_scope_for_source(
        kind="subsystem",
        source_id="wiki_generator",
        actor_id="wiki_consistency_critic",
        identity_overrides={"owner_id": PRINCIPAL_USER, "actor_id": "wiki_consistency_critic"},
    )


def _read_prose_page(vault_path: Path, entity_label: str) -> Optional[Dict[str, Any]]:
    path = vault_path / "prose" / f"{safe_filename(entity_label)}.md"
    if not path.exists():
        return None
    post = frontmatter.load(path)
    meta = post.metadata or {}
    return {
        "prose": post.content,
        "kg_node_id": meta.get("kg_node_id"),
        "node_type": meta.get("node_type") or "Entity",
        "path": str(path),
    }


def _build_ground_truth_block(entity_node_id: str, entity_label: str) -> str:
    """Assemble an authoritative-facts block from the entity_cards row +
    user_data resource (if the subject is the primary user). Anything we
    can assert with high confidence goes here — the critic treats this as
    ground truth and flags prose that disagrees with it.
    """
    lines: list[str] = []

    session = get_session()
    try:
        # Read summary + flattened bullets from the v2 card. This replaces
        # the v1 "SELECT summary, key_facts FROM entity_cards" query — v1
        # was retired 2026-05-10.
        from app.assistant.entity_management.entity_card_v2 import (
            EntityCardV2, EntityCardSection, EntityCardBullet, SUMMARY,
        )
        card = (
            session.query(EntityCardV2)
            .filter(
                EntityCardV2.entity_node_id == entity_node_id,
                EntityCardV2.is_active == True,  # noqa: E712
            )
            .one_or_none()
        )
        summary_text = None
        key_facts_list: list = []
        if card is not None:
            sections = (
                session.query(EntityCardSection)
                .filter(EntityCardSection.card_id == card.id)
                .order_by(EntityCardSection.position)
                .all()
            )
            for s in sections:
                if s.section_name == SUMMARY and s.intro_text:
                    summary_text = s.intro_text
                    continue
                bullets = (
                    session.query(EntityCardBullet)
                    .filter(EntityCardBullet.section_id == s.id)
                    .order_by(EntityCardBullet.position)
                    .all()
                )
                for b in bullets:
                    if b.bullet_text:
                        key_facts_list.append(b.bullet_text)
        row = (summary_text, key_facts_list) if card is not None else None
    except Exception as e:
        logger.warning("Could not read v2 entity card for %s: %s", entity_node_id, e)
        row = None
    finally:
        session.close()

    if row is not None:
        summary = (row[0] or "").strip()
        if summary:
            lines.append("Entity card summary:")
            lines.append(summary)
        key_facts_raw = row[1]
        if isinstance(key_facts_raw, str):
            # stored as JSON text in SQLite
            try:
                import json as _json
                key_facts_raw = _json.loads(key_facts_raw)
            except Exception:
                key_facts_raw = None
        if isinstance(key_facts_raw, list) and key_facts_raw:
            lines.append("")
            lines.append("Entity card key facts:")
            for kf in key_facts_raw:
                text = str(kf).strip().lstrip("-• ")
                if text:
                    lines.append(f"- {text}")

    # If this is the primary user, also pull home city / job from the user resource.
    try:
        user_data = DI.resource_manager.get_resource(
            scope_context=_scope(),
            resource_id="resource_user_data",
            required=False,
        )
    except Exception:
        user_data = None
    if isinstance(user_data, dict):
        preferred = str(user_data.get("preferred_name") or "").strip()
        if preferred and preferred.lower() == entity_label.lower():
            lines.append("")
            lines.append("From user profile (authoritative):")
            for field, label in [
                ("home_city", "Current home city"),
                ("home_state", "Current home state"),
                ("home_country", "Current home country"),
                ("job", "Current job"),
                ("birthdate", "Date of birth"),
            ]:
                val = str(user_data.get(field) or "").strip()
                if val:
                    lines.append(f"- {label}: {val}")
            important = user_data.get("important_people") or []
            if isinstance(important, list) and important:
                lines.append("- Important people:")
                for p in important:
                    if not isinstance(p, dict):
                        continue
                    name = p.get("name")
                    rel = p.get("relationship")
                    bd = p.get("birthdate")
                    if name and rel:
                        pretty = f"  - {name} ({rel}"
                        if bd:
                            pretty += f", born {bd}"
                        pretty += ")"
                        lines.append(pretty)

    return "\n".join(lines).strip()


def _locate_quote_context(prose: str, quoted: str) -> Dict[str, Any]:
    """Find the paragraph(s) in `prose` containing `quoted` and capture the
    surrounding heading. The investigator uses this to ground the contradiction
    without re-loading the whole wiki page.

    Returns {paragraph, section_heading, line_number} (best-effort; empty
    strings / None when not found). The quote can be a multi-snippet string
    separated by ' // ' from the critic's output convention; we try each
    snippet and take the first match.
    """
    out: Dict[str, Any] = {"paragraph": "", "section_heading": "", "line_number": None}
    if not prose or not quoted:
        return out

    # Critic may concatenate multiple snippets with ' // '; pick the first that
    # actually appears verbatim in the prose.
    snippets = [s.strip() for s in quoted.split(" // ") if s.strip()] or [quoted]
    found_at = -1
    for s in snippets:
        idx = prose.find(s)
        if idx >= 0:
            found_at = idx
            break
    if found_at < 0:
        return out

    # Paragraph = the contiguous block between the nearest blank lines before
    # and after the quote position.
    para_start = prose.rfind("\n\n", 0, found_at)
    para_start = 0 if para_start < 0 else para_start + 2
    para_end = prose.find("\n\n", found_at)
    para_end = len(prose) if para_end < 0 else para_end
    out["paragraph"] = prose[para_start:para_end].strip()

    # Section heading = the most recent line starting with '#'.
    head_idx = prose.rfind("\n#", 0, found_at)
    if head_idx >= 0:
        head_end = prose.find("\n", head_idx + 1)
        out["section_heading"] = prose[head_idx + 1:head_end if head_end >= 0 else len(prose)].strip()

    out["line_number"] = prose[:found_at].count("\n") + 1
    return out


def _save_finding(
    *,
    entity_node_id: str,
    entity_label: str,
    finding: Dict[str, Any],
    prose_path: str,
    prose_text: str,
) -> Optional[str]:
    """Persist one critic finding. Skips if an unresolved finding with the
    same quoted_text already exists for this node (dedup)."""
    session = get_session()
    try:
        quoted = (finding.get("quoted_text") or "").strip()
        existing = (
            session.query(KGMaintenanceFinding)
            .filter(
                KGMaintenanceFinding.finding_type == FINDING_TYPE,
                KGMaintenanceFinding.primary_node_id == entity_node_id,
                KGMaintenanceFinding.status == "pending",
            )
            .all()
        )
        for row in existing:
            ev = row.evidence_json or {}
            if (ev.get("quoted_text") or "").strip() == quoted:
                return None  # duplicate of a pending finding

        issue_type = str(finding.get("issue_type") or "other").strip()
        priority = "high" if issue_type in {"contradiction", "impossible_sequence"} else "medium"
        source_kind = str(finding.get("source_kind") or "").strip() or None
        source_statement = str(finding.get("source_statement") or "").strip() or None

        # Prepend the specific source into the reason so reviewers can see at
        # a glance which authoritative line is being disputed. For internal
        # contradictions the summary already stands alone.
        base_summary = str(finding.get("summary") or "")
        if source_kind and source_kind != "internal" and source_statement:
            source_label_map = {
                "entity_card_summary": "Entity card summary",
                "entity_card_key_fact": "Entity card key fact",
                "user_profile": "User profile",
            }
            pretty_source = source_label_map.get(source_kind, source_kind)
            reason_text = f"[{pretty_source}] {source_statement!r} — {base_summary}"
        else:
            reason_text = base_summary

        context = _locate_quote_context(prose_text, quoted)

        row = KGMaintenanceFinding(
            finding_type=FINDING_TYPE,
            status="pending",
            priority=priority,
            primary_node_id=entity_node_id,
            suggested_action="review",
            reason=reason_text[:1024],
            confidence=float(finding.get("confidence") or 0.0),
            agent_name=CRITIC_AGENT,
            evidence_json={
                "entity_label": entity_label,
                "issue_type": issue_type,
                "quoted_text": quoted,
                "prose_path": prose_path,
                "source_kind": source_kind,
                "source_statement": source_statement,
                # Surrounding context so the investigator can ground the
                # contradiction without reloading the whole wiki page.
                "wiki_paragraph": context["paragraph"],
                "wiki_section_heading": context["section_heading"],
                "wiki_line_number": context["line_number"],
            },
        )
        session.add(row)
        session.commit()
        return row.id
    except Exception as e:
        logger.error("Failed saving critic finding for %s: %s", entity_label, e)
        session.rollback()
        return None
    finally:
        session.close()


def run_consistency_critic(
    *,
    entity_label: str,
    vault_path: Path,
    investigate_immediately: bool = False,
    max_investigations: int = 5,
) -> Dict[str, Any]:
    """Run the critic on ``<vault>/prose/<entity_label>.md`` and file any
    findings. Returns a small summary dict (counts + saved finding ids).

    ``investigate_immediately`` defaults to False (audit P3.3, write-only):
    the nightly refresh runs the critic on UNBOUNDED pages — at up to 5
    immediate investigations per page that dwarfed the investigator's
    5/day budget AND starved the 03:30 cluster resolver of exactly the
    redundant per-entity findings it exists to distill. Nightly paths file
    findings only; the cluster resolver + backlog drain investigate the
    distilled leads under their own budgets. The manual single-page
    regenerate route passes True (one page, user is waiting, cost bounded
    by ``max_investigations``).
    """
    page = _read_prose_page(vault_path, entity_label)
    if page is None:
        return {"status": "no_prose_page", "findings": []}
    if not page.get("kg_node_id"):
        return {"status": "no_kg_node_id", "findings": []}

    agent = DI.agent_factory.create_agent(CRITIC_AGENT)
    if agent is None:
        raise RuntimeError(f"agent_factory returned None for {CRITIC_AGENT!r}")

    ground_truth_block = _build_ground_truth_block(
        entity_node_id=page["kg_node_id"],
        entity_label=entity_label,
    )
    msg = Message(
        agent_input={
            "entity_name": entity_label,
            "entity_type": page["node_type"],
            "prose": page["prose"],
            "ground_truth_block": ground_truth_block,
        },
        scope_context=_scope(),
    )
    result = agent.action_handler(msg)
    data = getattr(result, "data", None) or {}
    raw_findings = data.get("findings") or []
    if not isinstance(raw_findings, list):
        raw_findings = []

    saved_ids: List[str] = []
    for f in raw_findings:
        if not isinstance(f, dict):
            continue
        fid = _save_finding(
            entity_node_id=page["kg_node_id"],
            entity_label=entity_label,
            finding=f,
            prose_path=page["path"],
            prose_text=page["prose"],
        )
        if fid:
            saved_ids.append(fid)

    investigation_summary: Optional[Dict[str, Any]] = None
    if investigate_immediately and saved_ids:
        try:
            from app.assistant.kg_investigator.finding_processor import investigate_findings
            investigation_summary = investigate_findings(
                saved_ids, max_to_investigate=max_investigations,
            )
        except Exception as e:
            logger.error("auto-investigation failed for %s: %s", entity_label, e)
            logger.debug("auto-investigation exception details", exc_info=True)
            investigation_summary = {"status": "error", "error": str(e)}

    return {
        "status": "ok",
        "entity_label": entity_label,
        "kg_node_id": page["kg_node_id"],
        "findings_count": len(raw_findings),
        "saved_findings": saved_ids,
        "findings": raw_findings,
        "reason": data.get("reason"),
        "investigation_summary": investigation_summary,
    }
