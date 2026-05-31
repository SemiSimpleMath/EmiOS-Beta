"""Eval harness for me::edge_importance_rater (agent name retained for now).

Loads `edge_importance_eval.yaml` (real edges + expected scores), runs the
rater on them, and reports per-case predicted-vs-expected diff plus
aggregate MAE. Use this to iterate the prompt before kicking off a 5-hour
full pass.

Relocated from app/me/ to app/assistant/kg/ 2026-05-11 — edge importance
is general KG infrastructure (feeds card tagging, wiki, lens), not lens-only.

Usage:
    python -m app.assistant.kg.edge_importance_eval
    python -m app.assistant.kg.edge_importance_eval --few-shot 5
    # few-shot: first N cases go into the system prompt as worked examples,
    # remaining cases stay held out for measurement.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from app.assistant.kg.db.knowledge_graph_db_sqlite import Edge, Node
from app.assistant.utils.logging_config import get_logger
from app.assistant.importance.scoring import _build_edge_block
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

EVAL_YAML_PATH = Path(__file__).resolve().parent / "edge_importance_eval.yaml"


def load_eval_cases() -> List[dict]:
    """Load the YAML eval set."""
    if not EVAL_YAML_PATH.exists():
        raise FileNotFoundError(f"eval set not found: {EVAL_YAML_PATH}")
    data = yaml.safe_load(EVAL_YAML_PATH.read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError(f"'cases' must be a list in {EVAL_YAML_PATH}")
    return cases


def fetch_edges(
    case_ids: List[str],
) -> Tuple[Dict[str, Edge], Dict[str, Node]]:
    """Fetch the eval edges + every node referenced by them."""
    with get_db_manager().read_session() as session:
        edges = session.query(Edge).filter(Edge.id.in_(case_ids)).all()
        edge_by_id = {str(e.id): e for e in edges}
        node_ids = set()
        for e in edges:
            node_ids.add(str(e.source_id))
            node_ids.add(str(e.target_id))
        nodes = session.query(Node).filter(Node.id.in_(list(node_ids))).all()
        node_by_id = {str(n.id): n for n in nodes}
    return edge_by_id, node_by_id


def run_rater(
    cases: List[dict],
    edge_by_id: Dict[str, Edge],
    node_by_id: Dict[str, Node],
    few_shot_examples: Optional[List[dict]] = None,
) -> Dict[str, Tuple[float, str]]:
    """Send the eval cases to the rater and parse the scores out.

    `few_shot_examples` are NOT scored — they're sent in the system-prompt
    `information` field as worked examples for the model to follow.
    """
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.utils.pydantic_classes import Message

    prompt_lines: List[str] = []
    for c in cases:
        eid = str(c["id"])
        e = edge_by_id.get(eid)
        if e is None:
            print(f"  WARN: edge {eid} not in DB", file=sys.stderr)
            continue
        block = _build_edge_block(e, node_by_id)
        if block is None:
            print(f"  WARN: edge {eid} has missing endpoint", file=sys.stderr)
            continue
        prompt_lines.append(block)

    if not prompt_lines:
        return {}

    # Few-shot: prepend worked examples as `information`.
    information_text = ""
    if few_shot_examples:
        info_lines = ["## Worked examples — these are the gold standard.\n"]
        for c in few_shot_examples:
            eid = str(c["id"])
            e = edge_by_id.get(eid)
            if e is None:
                continue
            block = _build_edge_block(e, node_by_id)
            if block is None:
                continue
            info_lines.append(f"{block}\nEXPECTED SCORE: {c.get('expected', '?')}")
            notes = c.get("notes")
            if notes:
                info_lines.append(f"REASONING: {notes}")
            info_lines.append("")
        information_text = "\n".join(info_lines)

    scope = load_scope_for_source(
        kind="subsystem",
        source_id="edge_importance_eval",
        actor_id="me_edge_importance_eval",
        identity_overrides={"owner_id": "jukka"},
    )

    agent = DI.agent_factory.create_agent("me::edge_importance_rater")
    if agent is None:
        raise RuntimeError("me::edge_importance_rater agent unavailable")

    msg = Message(
        agent_input={
            "task": "\n".join(prompt_lines),
            "information": information_text,
        },
        task="\n".join(prompt_lines),
        information=information_text,
        scope_context=scope,
    )

    result = agent.action_handler(msg)
    data = getattr(result, "data", None) or {}
    ratings = data.get("ratings") or []

    scored: Dict[str, Tuple[float, str]] = {}
    for r in ratings:
        if not isinstance(r, dict):
            continue
        eid = str(r.get("id") or "").strip()
        try:
            score = float(r.get("score") or 5.0)
        except (TypeError, ValueError):
            continue
        reason = str(r.get("reason") or "").strip()
        scored[eid] = (max(0.0, min(10.0, score)), reason)
    return scored


def report(
    cases: List[dict],
    predicted: Dict[str, Tuple[float, str]],
) -> Tuple[float, int]:
    """Print per-case diff + return (MAE, num_scored)."""
    print()
    print(f"{'EXPECTED':>9}  {'PRED':>5}  {'DIFF':>5}  DESCRIPTION")
    print("-" * 100)
    diffs: List[float] = []
    for c in cases:
        eid = str(c["id"])
        exp = float(c.get("expected", 5.0))
        entry = predicted.get(eid)
        if entry is None:
            print(f"{exp:>9.1f}  {'NONE':>5}  {'?':>5}  {c.get('desc', eid)}")
            continue
        pred, reason = entry
        diff = pred - exp
        diffs.append(abs(diff))
        marker = ""
        if abs(diff) >= 3.0:
            marker = "  ⚠️ off by ≥3"
        elif abs(diff) >= 1.5:
            marker = "  → significant"
        print(f"{exp:>9.1f}  {pred:>5.1f}  {diff:>+5.1f}  {c.get('desc', eid)}{marker}")
        if reason and abs(diff) >= 1.5:
            print(f"    reason: {reason}")
    if not diffs:
        return 0.0, 0
    mae = sum(diffs) / len(diffs)
    print()
    print(f"MAE over {len(diffs)} cases: {mae:.2f}")
    print(f"Cases off by ≥1.5: {sum(1 for d in diffs if d >= 1.5)}")
    print(f"Cases off by ≥3.0: {sum(1 for d in diffs if d >= 3.0)}")
    return mae, len(diffs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--few-shot", type=int, default=0,
        help="Use first N cases as in-prompt worked examples (held out from eval).",
    )
    args = parser.parse_args()

    all_cases = load_eval_cases()
    print(f"Loaded {len(all_cases)} eval cases.")

    if args.few_shot > 0:
        few_shot = all_cases[: args.few_shot]
        eval_cases = all_cases[args.few_shot :]
        print(f"Using first {len(few_shot)} as few-shot examples; eval on remaining {len(eval_cases)}.")
    else:
        few_shot = None
        eval_cases = all_cases

    case_ids = [c["id"] for c in all_cases]  # fetch all (eval + few-shot)
    edge_by_id, node_by_id = fetch_edges(case_ids)
    missing = [c for c in eval_cases if c["id"] not in edge_by_id]
    if missing:
        print(f"WARN: {len(missing)} eval cases missing in DB — skipping.")

    predicted = run_rater(eval_cases, edge_by_id, node_by_id, few_shot_examples=few_shot)
    mae, n = report(eval_cases, predicted)


if __name__ == "__main__":
    import app.assistant.tests.test_setup  # noqa: F401
    main()
