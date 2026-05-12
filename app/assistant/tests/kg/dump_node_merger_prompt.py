"""Dump the EXACT system + user prompts the node_merger LLM would see for a
single State/Event/Goal merge decision, using real data from the live KG.

Runs the same code paths production uses to build agent_input, then renders
the agent's Jinja templates with that input. Stops *before* the LLM call —
prints the rendered prompts and exits.

Why this exists: 2026-05-03 — investigating why a Drowsy Chaperone proposal
got merged into a Beetlejuice-era Performance hub. We need to see the
merger's actual prompt with all variables resolved to understand the LLM's
decision context.

Usage::

    .venv\\Scripts\\python.exe app/assistant/tests/kg/dump_node_merger_prompt.py

Optional args (positional):
    1: candidate node id prefix (default: 0b4416dd  — the Performance hub)
    2: proposal node id prefix  (default: f88f82ac  — the 2026-05-03 Drowsy
        Chaperone proposal that was merged into Performance)
"""
from __future__ import annotations

import json
import sys

import app.assistant.tests.test_setup  # noqa: F401  — bootstraps DI

from sqlalchemy import text as sql_text

from app.assistant.kg.proposal_promoter import _snapshot_state_candidate
from app.assistant.kg.db.knowledge_graph_db_sqlite import Node
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.utils.pydantic_classes import Message
from app.models.db_manager import get_db_manager


DEFAULT_CANDIDATE_PREFIX = "0b4416dd"  # Performance hub
DEFAULT_PROPOSAL_PREFIX = "f88f82ac"   # 2026-05-03 Drowsy Chaperone proposal


def _resolve_full_id(s, prefix: str, table: str) -> str:
    rows = s.execute(sql_text(
        f"SELECT id FROM {table} WHERE id LIKE :p LIMIT 2"
    ), {"p": prefix + "%"}).fetchall()
    if not rows:
        raise SystemExit(f"No row in {table} with id prefix {prefix!r}")
    if len(rows) > 1:
        raise SystemExit(f"Ambiguous prefix {prefix!r} in {table} ({len(rows)} matches)")
    return rows[0].id


def _build_new_node_ctx_from_proposal(s, prop_node_id: str) -> dict:
    """Build the new_node_context payload the same way _prepare_proposal_plan
    does — minus the live LLM-derived TTL/canonical_sentence (we use the
    proposal's recorded sentence directly)."""
    pn = s.execute(sql_text(
        """
        SELECT id, label, node_type, category, description_draft, sentence,
               valid_from, valid_to, valid_from_prose, valid_to_prose,
               attributes_json
        FROM claim_proposal_node
        WHERE id = :i
        """
    ), {"i": prop_node_id}).fetchone()
    if pn is None:
        raise SystemExit(f"claim_proposal_node {prop_node_id} not found")

    # Find the proposal's window for source_window_text + end ts.
    win = s.execute(sql_text(
        """
        SELECT cpe.window_id
        FROM claim_proposal_evidence cpe
        WHERE cpe.proposal_id = (
            SELECT proposal_id FROM claim_proposal_node WHERE id = :i
        )
        LIMIT 1
        """
    ), {"i": prop_node_id}).fetchone()
    window_id = win.window_id if win else None

    from app.assistant.kg.proposal_promoter import (
        _node_window_text, _window_end_ts,
    )
    window_text = None
    if window_id:
        # Fetch via the same helper used for candidate windows. We don't
        # have a node_id yet (proposal hasn't been resolved), so duplicate
        # the SQL the helper does for windows directly:
        rows = s.execute(sql_text(
            """
            SELECT ul.role, ul.speaker_name, ul.message
            FROM kg_window_message wm
            JOIN unified_log_2026 ul ON ul.id = wm.unified_log_id
            WHERE wm.window_id = :w
            ORDER BY wm.item_order
            """
        ), {"w": window_id}).fetchall()
        lines = []
        for r in rows:
            speaker = r.speaker_name or r.role or "?"
            msg = (r.message or "").strip()
            if msg:
                lines.append(f"{speaker}: {msg}")
        window_text = ("\n".join(lines))[:600]

    attrs = pn.attributes_json
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}
    if not isinstance(attrs, dict):
        attrs = {}
    ttl = attrs.get("ttl") if isinstance(attrs, dict) else None

    def _iso(v):
        if v is None: return None
        if isinstance(v, str): return v
        iso = getattr(v, "isoformat", None)
        return iso() if callable(iso) else str(v)

    return {
        "label": pn.label,
        "node_type": pn.node_type,
        "category": pn.category,
        "description": (pn.description_draft or ""),
        "original_sentence": pn.sentence,
        "valid_from": _iso(pn.valid_from),
        "valid_to": _iso(pn.valid_to),
        "valid_from_prose": pn.valid_from_prose,
        "valid_to_prose": pn.valid_to_prose,
        "source_window_id": window_id,
        "source_window_end_ts": _window_end_ts(s, window_id) if window_id else None,
        "source_window_text": window_text,
        # first_observed = valid_from on the proposal node (no longer in JSON).
        "first_observed": _iso(pn.valid_from),
        "ttl_duration_class": (ttl.get("duration_class") if isinstance(ttl, dict) else None),
    }


def main() -> None:
    cand_prefix = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CANDIDATE_PREFIX
    prop_prefix = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_PROPOSAL_PREFIX

    # Bootstrap services + agents (needed so PromptBuilder / DI exist).
    from app.assistant.tests.test_setup import initialize_services
    try:
        initialize_services()
    except Exception:
        pass

    with get_db_manager().read_session() as s:
        cand_id = _resolve_full_id(s, cand_prefix, "kg_node_metadata")
        prop_id = _resolve_full_id(s, prop_prefix, "claim_proposal_node")

        cand_node = s.query(Node).filter(Node.id == cand_id).first()
        if cand_node is None:
            raise SystemExit(f"kg_node_metadata {cand_id} not found")

        cand_payload = _snapshot_state_candidate(s, cand_node)
        new_node_ctx = _build_new_node_ctx_from_proposal(s, prop_id)

    print("=" * 78)
    print(f"  EXISTING CANDIDATE  id={cand_id[:8]}  type={cand_node.node_type}  label={cand_node.label!r}")
    print(f"  NEW PROPOSAL        id={prop_id[:8]}  type={new_node_ctx['node_type']}  label={new_node_ctx['label']!r}")
    print("=" * 78)
    print()

    # Build the agent + render its prompts (no LLM call).
    agent = DI.agent_factory.create_agent("knowledge_graph_add::node_merger")
    if agent is None:
        raise SystemExit("agent_factory could not create node_merger — DI not bootstrapped?")

    scope = build_pipeline_scope_context(
        pipeline_id="kg_pipeline", actor_id="proposal_promoter",
    )
    agent_input = {
        "new_node_context": json.dumps(new_node_ctx, ensure_ascii=True, indent=2),
        "existing_node_candidates": json.dumps([cand_payload], ensure_ascii=True, indent=2),
    }
    msg = Message(agent_input=agent_input, scope_context=scope)

    # The context_injector reads scope_context AND user_context_items from
    # the agent's blackboard (action_handler normally seeds these from the
    # incoming Message). For a stand-alone render, stash them ourselves.
    agent.blackboard.update_state_value("scope_context", scope)
    for k, v in agent_input.items():
        agent.blackboard.update_state_value(k, v)

    # PromptBuilder is the same one the runtime uses. Renders both prompts
    # with all Jinja variables resolved against the live resources.
    from app.assistant.agent_runtime.services.prompt_builder import PromptBuilder
    pb = PromptBuilder()
    system_prompt = pb.get_system_prompt(agent, msg, set())
    user_prompt = pb.get_user_prompt(agent, msg, set())

    print("─" * 78)
    print("RENDERED SYSTEM PROMPT")
    print("─" * 78)
    print(system_prompt)
    print()
    print("─" * 78)
    print("RENDERED USER PROMPT")
    print("─" * 78)
    print(user_prompt)


if __name__ == "__main__":
    main()
