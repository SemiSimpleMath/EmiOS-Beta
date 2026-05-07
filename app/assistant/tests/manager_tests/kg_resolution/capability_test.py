"""kg_resolution_manager — capability tests across read / judgment / repair.

Real-LLM, real-KG integration tests. Each test exercises one capability
of the manager and prints the resulting report + tool-call summary.

Categories:
  READ-ONLY     — investigation tasks (counts, lookups, fact verification)
  JUDGMENT      — schema-aware decisions (recurring-event collapse?)
  META-DEBUG    — why does another agent think X (false-positive triage)
  REPAIR        — full mutation + regen + finding-resolve flow

WARNING: tests in the REPAIR category mutate the live KG (close/update
State nodes, regenerate entity cards, refresh wiki pages). Run only
when you've backed up emi.db or are explicitly testing destructive
paths. The default `run all` behavior runs every test in declared
order, including REPAIR tests at the end.

Usage:
    # List available tests:
    .venv/Scripts/python.exe -m app.assistant.tests.manager_tests.kg_resolution.capability_test --list

    # Run a single test by name:
    .venv/Scripts/python.exe -m app.assistant.tests.manager_tests.kg_resolution.capability_test orphan_count

    # Run all read-only + judgment tests (skips REPAIR):
    .venv/Scripts/python.exe -m app.assistant.tests.manager_tests.kg_resolution.capability_test --safe

    # Run literally everything:
    .venv/Scripts/python.exe -m app.assistant.tests.manager_tests.kg_resolution.capability_test --all
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import app.assistant.tests.test_setup  # noqa: F401  — bootstraps DI
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.pydantic_classes import (
    Message,
    ScopeApprovalPolicy,
    ScopeContext,
    ScopeResourcePolicy,
    ScopeWritePolicy,
)


MANAGER_NAME = "kg_resolution_manager"


# ---------------------------------------------------------------------
# Test harness — one helper invokes the manager and returns the report.
# ---------------------------------------------------------------------

def _scope(*, write_kg: bool) -> ScopeContext:
    """Per-test scope. Read-only tests use write_kg=False as a belt-and-
    suspenders guard against accidental mutation; REPAIR tests pass True."""
    return ScopeContext(
        scope_id=f"scope::test::kg_resolution::{int(time.time() * 1000)}",
        owner_id="jukka",
        actor_id="kg_resolution_manager_capability_test",
        surface="test",
        approval=ScopeApprovalPolicy(authority_level=100),
        resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
        writes=ScopeWritePolicy(write_kg=write_kg, write_unified_log=False),
    )


def _invoke(
    *,
    task: str,
    information: str,
    write_kg: bool,
) -> Dict[str, Any]:
    """Invoke kg_resolution_manager once with the given task. Returns a
    summary dict — the structured report, total wall-clock, the count
    of tool calls, and any mutator names that fired."""
    DI.manager_registry.preload_all()
    manager = DI.multi_agent_manager_factory.create_manager(MANAGER_NAME)
    if manager is None:
        return {"error": f"manager {MANAGER_NAME} not registered"}

    msg = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task,
        information=information,
        scope_context=_scope(write_kg=write_kg),
    )

    started = time.time()
    try:
        manager.request_handler(msg)
    except Exception as e:
        return {
            "error": f"manager invocation crashed: {e!r}",
            "elapsed_s": round(time.time() - started, 2),
        }
    elapsed = round(time.time() - started, 2)

    # Walk the blackboard for the structured report and tool-call audit.
    report: Optional[Dict[str, Any]] = None
    tool_calls: List[str] = []
    mutator_calls: List[str] = []
    try:
        msgs = manager.blackboard.get_messages()
    except Exception:
        msgs = []

    MUTATORS = {
        "kg_close_state", "kg_create_state_node", "kg_update_node_field",
        "kg_rename_label", "kg_create_edge", "kg_delete_edge", "kg_delete_node",
        "regenerate_entity_card", "refresh_wiki_page",
        "kg_finding_resolve", "kg_finding_escalate",
    }

    for m in msgs:
        sender = str(getattr(m, "sender", "") or "")
        data = getattr(m, "data", None) or {}

        if sender.endswith("final_answer") and "kg_resolution" in sender:
            report = {
                k: data.get(k) for k in (
                    "summary", "user_prose", "mutations", "regenerations",
                    "findings_resolved", "open_questions", "completed",
                    "final_answer_answer", "result_summary",
                ) if data.get(k) is not None
            }

        # Tool-caller messages typically carry an action / target_name.
        target = data.get("action") or data.get("target_name")
        if isinstance(target, str) and target:
            tool_calls.append(target)
            if target in MUTATORS:
                mutator_calls.append(target)

    return {
        "elapsed_s": elapsed,
        "report": report,
        "tool_calls": tool_calls,
        "tool_call_count": len(tool_calls),
        "mutator_calls": mutator_calls,
    }


def _print_result(test_name: str, result: Dict[str, Any]) -> None:
    print(f"\n{'=' * 72}")
    print(f"  {test_name}")
    print(f"{'=' * 72}")
    if "error" in result:
        print(f"  ERROR: {result['error']}")
        if "elapsed_s" in result:
            print(f"  elapsed: {result['elapsed_s']}s")
        return
    print(f"  elapsed:        {result['elapsed_s']}s")
    print(f"  tool calls:     {result['tool_call_count']}")
    if result["tool_calls"]:
        print(f"  tool sequence:  {', '.join(result['tool_calls'])}")
    if result["mutator_calls"]:
        print(f"  ⚠ mutators fired: {', '.join(result['mutator_calls'])}")
    report = result.get("report") or {}
    if report:
        print(f"\n  ----- structured report -----")
        for k, v in report.items():
            if isinstance(v, str) and len(v) > 600:
                v = v[:600] + " …(truncated for display only)"
            print(f"  {k}: {v}")
    else:
        print("  (no final-answer report on blackboard — manager may have exited early)")


# ---------------------------------------------------------------------
# READ-ONLY tests — investigation only; mutation should not fire.
# ---------------------------------------------------------------------

def test_orphan_count() -> Dict[str, Any]:
    """Count orphan nodes (no incoming or outgoing edges)."""
    return _invoke(
        task="Count the orphan nodes in the knowledge graph and report the total. Include a small sample (5-10) of orphan node ids and labels in your report.",
        information=(
            "An orphan node has zero edges — no row in kg_edge_metadata "
            "where its id appears as source_id or target_id. Use kg_query "
            "with a NOT EXISTS subquery against kg_edge_metadata to count, "
            "then a small SELECT to fetch sample rows. Do not mutate. "
            "Return the count and sample in your final answer."
        ),
        write_kg=False,
    )


def test_martin_mention_count() -> Dict[str, Any]:
    """Count nodes mentioning 'Martin'."""
    return _invoke(
        task="Count the nodes that mention 'Martin' anywhere in their label, original_sentence, or aliases. Group the count by node_type in your final answer.",
        information=(
            "Use a single kg_query with case-insensitive LIKE on label, "
            "original_sentence, and the aliases JSON column. Group results "
            "by node_type. Do not mutate."
        ),
        write_kg=False,
    )


def test_who_is_jorma() -> Dict[str, Any]:
    """Identify Jorma from the KG."""
    return _invoke(
        task="Look up the entity 'Jorma' in the knowledge graph and describe who they are based on the most important States, Events, and Properties associated with them. Cite the supporting node ids in your answer.",
        information=(
            "Jorma is an Entity node. Find it via kg_query (LIKE on label "
            "and aliases). Then read its highest-importance edges and the "
            "States/Events/Properties on the other end. Synthesize a short "
            "biographical summary. Do not mutate."
        ),
        write_kg=False,
    )


def test_jukka_scottsdale_current() -> Dict[str, Any]:
    """Verify whether Jukka currently lives in Scottsdale."""
    return _invoke(
        task="Is it true that Jukka currently lives in Scottsdale? Answer yes or no based on the knowledge graph, and cite the State node and validity window that supports your answer.",
        information=(
            "Use the principles in resource_kg_principles: a State is "
            "'currently true' iff end_date IS NULL (or in the future) AND "
            "start_date is set or null. Find the relevant lives_in / "
            "residence State for Jukka pointing to Scottsdale. Read the "
            "dates. Answer based on the dates, not the present-tense form. "
            "Do not mutate."
        ),
        write_kg=False,
    )


# ---------------------------------------------------------------------
# JUDGMENT tests — schema-aware decisions, no mutation expected.
# ---------------------------------------------------------------------

def test_friday_meats_collapse_recommendation() -> Dict[str, Any]:
    """Should recurring 'Friday Night Meats' Events be collapsed?"""
    return _invoke(
        task="There are multiple 'Friday Night Meats' Event nodes in the knowledge graph. Investigate and recommend whether to collapse them into a single node. Cite the principle from resource_kg_principles that supports your recommendation.",
        information=(
            "Find all Event nodes whose label is 'Friday Night Meats' or "
            "similar. Read their start_date / end_date for each. Apply the "
            "recurring-event principle from resource_kg_principles (the "
            "2026-05-07 trap): same-label, same-subject Events with "
            "different date windows are separate occurrences, not "
            "duplicates. Recommend keep-as-is unless dates strongly "
            "indicate genuine duplication. Do not mutate; this is a "
            "recommendation only. The kg_merge_nodes tool is gated against "
            "this case anyway — escalate if you think a real merge is "
            "warranted."
        ),
        write_kg=False,
    )


# ---------------------------------------------------------------------
# META-DEBUG tests — diagnose another agent's claim of contradiction.
# ---------------------------------------------------------------------

def test_jukka_scottsdale_contradiction_diagnosis() -> Dict[str, Any]:
    """Diagnose: why does some investigator think 'Jukka lives in Scottsdale' is contradictory?"""
    return _invoke(
        task="An investigator agent has flagged a contradiction involving the claim 'Jukka lives in Scottsdale'. Investigate the relevant State / Event / Entity nodes and explain (a) whether the contradiction is real, (b) what the investigator likely misunderstood if it is not real, OR (c) what the actual KG inconsistency is if it is real.",
        information=(
            "Apply the conflict-triage skill: read the underlying nodes, "
            "check validity windows, check for present-tense canonical "
            "misreading, check for residence sequence (multiple lives_in "
            "States with non-overlapping windows = sequential residences, "
            "not contradictions). Walk the three buckets — real_kg_issue / "
            "real_derivative_staleness / investigator_misunderstanding — "
            "and pick the right one with cited evidence. Do not mutate."
        ),
        write_kg=False,
    )


# ---------------------------------------------------------------------
# REAL-DATA META-DEBUG — pull every pending wiki_contradiction finding
# from the live KG and triage each one through the manager. write_kg=False
# so the manager produces verdicts without applying mutations.
# ---------------------------------------------------------------------

def test_wiki_contradictions_verify_all() -> Dict[str, Any]:
    """Triage every pending wiki_contradiction finding without mutating."""
    from app.models.base import get_session
    from sqlalchemy import text as sql_text

    s = get_session()
    try:
        rows = s.execute(sql_text("""
            SELECT id, primary_node_id, primary_label, primary_node_type,
                   reason, evidence_json
            FROM kg_maintenance_finding
            WHERE finding_type = 'wiki_contradiction'
              AND status = 'pending'
              AND superseded_by IS NULL
            ORDER BY created_at DESC
        """)).fetchall()
    finally:
        s.close()

    if not rows:
        return {"elapsed_s": 0.0, "report": {"summary": "no pending wiki_contradiction findings"},
                "tool_calls": [], "tool_call_count": 0, "mutator_calls": []}

    print(f"  found {len(rows)} pending wiki_contradiction findings to triage")
    aggregate_elapsed = 0.0
    verdicts: List[Dict[str, Any]] = []
    aggregate_tool_calls: List[str] = []
    aggregate_mutator_calls: List[str] = []
    started = time.time()

    for i, row in enumerate(rows, 1):
        finding_id = str(row[0])
        primary_node_id = str(row[1] or "")
        primary_label = str(row[2] or "")
        primary_node_type = str(row[3] or "")
        reason = str(row[4] or "")
        ev = row[5]
        # evidence_json comes as either a parsed dict or a JSON string,
        # depending on dialect/driver. Render it as a string for the prompt.
        if isinstance(ev, (dict, list)):
            import json
            ev_str = json.dumps(ev, ensure_ascii=False, default=str)
        else:
            ev_str = str(ev or "")

        print(f"\n  [{i}/{len(rows)}] finding={finding_id[:8]} node={primary_label!r} ({primary_node_type})")
        sub = _invoke(
            task=(
                f"Triage the following wiki_contradiction finding. Decide which "
                f"of the three buckets it falls into per the kg-conflict-triage "
                f"skill: real_kg_issue, real_derivative_staleness, or "
                f"investigator_misunderstanding. Cite the underlying node ids "
                f"and dates in your reasoning. Do NOT mutate; this run is "
                f"verification only."
            ),
            information=(
                f"Finding id: {finding_id}\n"
                f"Primary node: {primary_label!r} (id={primary_node_id}, "
                f"type={primary_node_type})\n"
                f"Critic reason: {reason}\n"
                f"Critic evidence: {ev_str}\n\n"
                f"Investigate via kg_query, apply the conflict-triage flow, "
                f"and return a verdict in your final answer along with: "
                f"the chosen bucket, a one-line conclusion, and a "
                f"machine-readable suggested action "
                f"(dismiss / regen / mutate-and-regen / escalate)."
            ),
            write_kg=False,
        )

        verdicts.append({
            "finding_id": finding_id[:8],
            "primary_label": primary_label,
            "elapsed_s": sub.get("elapsed_s"),
            "tool_call_count": sub.get("tool_call_count"),
            "mutators": sub.get("mutator_calls"),
            "report_summary": (sub.get("report") or {}).get("summary"),
            "report_final_answer": (sub.get("report") or {}).get("final_answer_answer"),
        })
        aggregate_elapsed += sub.get("elapsed_s") or 0.0
        aggregate_tool_calls.extend(sub.get("tool_calls") or [])
        aggregate_mutator_calls.extend(sub.get("mutator_calls") or [])
        print(f"      → {sub.get('elapsed_s')}s, {sub.get('tool_call_count')} tool calls"
              + (f", ⚠ mutators: {sub.get('mutator_calls')}" if sub.get("mutator_calls") else ""))

    return {
        "elapsed_s": round(time.time() - started, 2),
        "report": {
            "summary": f"Triaged {len(verdicts)} pending wiki_contradiction findings",
            "verdicts": verdicts,
        },
        "tool_calls": aggregate_tool_calls,
        "tool_call_count": len(aggregate_tool_calls),
        "mutator_calls": aggregate_mutator_calls,
    }


def test_dylan_jukka_nephew() -> Dict[str, Any]:
    """Verify whether Dylan is Jukka's nephew (multi-hop, may have no direct edge)."""
    return _invoke(
        task=(
            "Is Dylan really Jukka's nephew? Investigate the knowledge graph "
            "and answer based on what's actually there. The relationship may "
            "not exist as a direct edge — check transitively via siblings + "
            "children (Dylan's parent is Jukka's sibling → Dylan is Jukka's "
            "nephew). Be honest about what's present vs missing."
        ),
        information=(
            "Find the Entity nodes for Dylan and Jukka. Look for edges "
            "between them directly (predicate ~ 'nephew', 'uncle'). If no "
            "direct edge, check if Dylan has a parent who is also Jukka's "
            "sibling — that would make Dylan Jukka's nephew transitively. "
            "If neither path is present in the KG, say so honestly: 'No "
            "edge or transitive path supports this in the current KG.' "
            "Do not mutate or fabricate."
        ),
        write_kg=False,
    )


# ---------------------------------------------------------------------
# REPAIR tests — full mutation + regeneration + finding lifecycle.
# ⚠ MUTATES THE LIVE KG. Run only with --all or by name.
# ---------------------------------------------------------------------

def test_annika_art_classes_august_repair() -> Dict[str, Any]:
    """Annika stopped art classes in August 2025 (not November). Full repair."""
    return _invoke(
        task=(
            "Annika actually stopped going to art classes in August 2025, "
            "not November 2025. Find every place in the KG that needs to "
            "be updated, apply the smallest correct mutations, and decide "
            "whether the wiki entry for Annika and her entity card need to "
            "be regenerated. Report what you changed and why."
        ),
        information=(
            "Apply the conflict-triage skill: read the affected State "
            "node(s) about Annika + art classes; check their current "
            "end_date and end_date_confidence. If the current end_date is "
            "November 2025, update it to August 2025 with prose 'August "
            "2025' and confidence='user_set' (the user is the authoritative "
            "source for this correction). Then evaluate per the wiki and "
            "entity-card principles: would the bullets / key_facts render "
            "differently after the date change? If yes, regenerate; if "
            "no, skip. Resolve any related findings. Don't blanket-refresh "
            "neighbors; only Annika-anchored derivatives unless others "
            "actually depend on the changed fact."
        ),
        write_kg=True,
    )


# ---------------------------------------------------------------------
# Test registry + CLI
# ---------------------------------------------------------------------

# (name, function, category) — declared in the order we want them to run.
TESTS: List[Tuple[str, Callable[[], Dict[str, Any]], str]] = [
    ("orphan_count",                          test_orphan_count,                          "READ-ONLY"),
    ("martin_count",                          test_martin_mention_count,                  "READ-ONLY"),
    ("who_is_jorma",                          test_who_is_jorma,                          "READ-ONLY"),
    ("jukka_scottsdale_current",              test_jukka_scottsdale_current,              "READ-ONLY"),
    ("dylan_jukka_nephew",                    test_dylan_jukka_nephew,                    "READ-ONLY"),
    ("friday_meats_collapse",                 test_friday_meats_collapse_recommendation,  "JUDGMENT"),
    ("jukka_scottsdale_contradiction",        test_jukka_scottsdale_contradiction_diagnosis, "META-DEBUG"),
    ("wiki_contradictions_verify_all",        test_wiki_contradictions_verify_all,        "META-DEBUG"),
    ("annika_art_classes_august",             test_annika_art_classes_august_repair,      "REPAIR"),
]

_BY_NAME = {name: (fn, cat) for name, fn, cat in TESTS}


def _run(name: str) -> bool:
    fn, cat = _BY_NAME[name]
    print(f"\n[{cat}] {name} — starting…")
    result = fn()
    _print_result(name, result)
    return "error" not in result


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    if args[0] == "--list":
        for name, _, cat in TESTS:
            print(f"  [{cat:11s}] {name}")
        return 0
    if args[0] == "--safe":
        names = [n for n, _, c in TESTS if c != "REPAIR"]
    elif args[0] == "--all":
        names = [n for n, _, _ in TESTS]
    else:
        names = []
        for arg in args:
            if arg in _BY_NAME:
                names.append(arg)
            else:
                print(f"unknown test: {arg!r}", file=sys.stderr)
                print(f"available: {', '.join(_BY_NAME)}", file=sys.stderr)
                return 2

    started = time.time()
    failures = 0
    for name in names:
        ok = _run(name)
        if not ok:
            failures += 1
    elapsed = round(time.time() - started, 2)

    print(f"\n{'=' * 72}")
    print(f"  ran {len(names)} tests in {elapsed}s — {failures} failures")
    print(f"{'=' * 72}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
