"""Explicit-path evaluation against SQLite copies; no production store or vector writes.

The model chooses context and revisions. This module enforces identities, source
references, owner locks, transactionality and optimistic concurrency only.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def readonly(path: Path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def index_references(context: dict) -> None:
    """Give sources short, stable citation handles without changing source identity."""
    mapping = context.setdefault('source_handles', {})
    inverse = {v:k for k,v in mapping.items()}
    records = [e for p in context['inspected_beliefs'] for e in p['evidence']]
    records += [m for d in context['source_days'] for m in d['messages']]
    records += [t for d in context['source_days'] for t in d.get('tickets', [])]
    records += context.get('insights', [])
    for record in records:
        original = record.get('original_ref', record['ref'])
        if original not in inverse:
            handle = f's{len(mapping)+1}'
            mapping[handle] = original
            inverse[original] = handle
        record['original_ref'] = original
        record['ref'] = inverse[original]


def make_working_copy(baseline: Path, destination: Path) -> None:
    baseline, destination = baseline.resolve(), destination.resolve()
    if destination.exists() or baseline == destination:
        raise ValueError("Destination must be a new file distinct from the baseline")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with readonly(baseline) as source, sqlite3.connect(destination) as target:
        source.backup(target)
        target.executescript('''
            CREATE TABLE contextual_review_copy (baseline TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE contextual_review_runs (
                run_id TEXT PRIMARY KEY, decision_hash TEXT NOT NULL, decision_json TEXT NOT NULL,
                context_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE contextual_review_revisions (
                run_id TEXT NOT NULL, belief_id TEXT NOT NULL,
                before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                rationale_json TEXT NOT NULL, PRIMARY KEY(run_id, belief_id));
        ''')
        target.execute("INSERT INTO contextual_review_copy VALUES (?, ?)",
                       (str(baseline), datetime.now(timezone.utc).isoformat()))


class ReviewCopy:
    def __init__(self, baseline: Path, working: Path):
        self.baseline, self.working = baseline.resolve(), working.resolve()
        if self.baseline == self.working:
            raise ValueError("Baseline is immutable")
        with readonly(self.working) as conn:
            marker = conn.execute("SELECT baseline FROM contextual_review_copy").fetchall()
            if len(marker) != 1 or Path(marker[0][0]).resolve() != self.baseline:
                raise ValueError("Not a matching review copy")

    def catalog(self) -> list[dict]:
        with readonly(self.working) as conn:
            return [dict(r) for r in conn.execute(
                "SELECT id, belief_key, statement, domain, status, locked, last_confirmed "
                "FROM user_beliefs ORDER BY belief_key")]

    def inspect(self, ids: list[str]) -> list[dict]:
        packets = []
        with readonly(self.working) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for belief_id in dict.fromkeys(ids):
                row = conn.execute("SELECT * FROM user_beliefs WHERE id=?", (belief_id,)).fetchone()
                archived = False
                if row is None and 'user_beliefs_archive' in tables:
                    row = conn.execute("SELECT * FROM user_beliefs_archive WHERE id=?", (belief_id,)).fetchone()
                    archived = row is not None
                if row is None:
                    raise ValueError(f"Unknown belief ID: {belief_id}")
                evidence = []
                evidence_table = 'belief_evidence_archive' if archived else 'belief_evidence'
                for ev in conn.execute(f"SELECT * FROM {evidence_table} WHERE belief_id=? ORDER BY source_date,id", (belief_id,)):
                    item = dict(ev)
                    item["ref"] = ("archived_evidence:" if archived else "evidence:") + item["id"]
                    evidence.append(item)
                predecessors = []
                if 'belief_merges' in tables:
                    for merge in conn.execute("SELECT loser_id,reason FROM belief_merges WHERE survivor_id=?", (belief_id,)):
                        predecessors.append({"belief_id": merge[0], "legacy_merge_reason": merge[1]})
                packets.append({"belief": dict(row), "version": digest(dict(row)), "evidence": evidence,
                                "archived": archived, "merged_predecessors": predecessors})
        return packets

    def source_day(self, day: str) -> dict:
        start = date.fromisoformat(day)
        end = start + timedelta(days=1)
        with readonly(self.baseline) as conn:
            rows = conn.execute(
                "SELECT id,timestamp,role,message,speaker_id,speaker_name,source FROM unified_log_2026 "
                "WHERE room_id='master_room' AND role IN ('user','assistant') "
                "AND timestamp>=? AND timestamp<? ORDER BY timestamp,id",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tickets = []
            if 'tickets' in tables:
                tickets = [dict(r, ref='ticket:' + str(r['ticket_id'])) for r in conn.execute(
                    "SELECT ticket_id,title,message,state,state_history,user_action,user_text,user_response_parsed,"
                    "created_at,responded_at,completed_at FROM tickets WHERE "
                    "(created_at>=? AND created_at<?) OR (responded_at>=? AND responded_at<?) ORDER BY created_at,ticket_id",
                    (start.isoformat(),end.isoformat(),start.isoformat(),end.isoformat()))]
        return {"date_utc": day, "messages": [dict(r, ref="chat:" + str(r["id"])) for r in rows],
                "tickets": tickets,
                "coverage": "Master-room user/assistant messages and tickets created or answered on this UTC day in the frozen backup. No other rooms or deleted sources."}

    def apply(self, run_id: str, decision: dict, context: dict) -> dict:
        """Commit model-authored changes and complete before/after receipts atomically."""
        from app.assistant.agents.belief_engine.context_review.agent_form import AgentForm
        decision = AgentForm.model_validate(decision).model_dump()
        if decision["phase"] != "finish" or any(decision[k] for k in
                ("search_queries", "inspect_belief_ids", "source_dates")):
            raise ValueError("Investigation is incomplete")
        packets = {p["belief"]["id"]: p for p in context["inspected_beliefs"]}
        focal = set(context["focal_belief_ids"])
        allowed_refs = {e["ref"] for p in packets.values() for e in p["evidence"]}
        allowed_refs.update(m["ref"] for d in context["source_days"] for m in d["messages"])
        allowed_refs.update(t["ref"] for d in context["source_days"] for t in d.get("tickets", []))
        allowed_refs.update(s["ref"] for s in context.get("insights", []))
        accounted = {r["belief_id"] for r in decision["revisions"]}
        for f in decision["preserved"] + decision["unresolved"]:
            accounted.update(f["belief_ids"])
            if not set(f["belief_ids"]) <= packets.keys() or not set(f["evidence_refs"]) <= allowed_refs:
                raise ValueError("Finding cites uninspected material: " + canonical({
                    "belief_ids": sorted(set(f['belief_ids']) - packets.keys()),
                    "evidence_refs": sorted(set(f['evidence_refs']) - allowed_refs)}))
        if not focal <= accounted:
            raise ValueError("Every focal belief must be accounted for")
        changed_ids = [r["belief_id"] for r in decision["revisions"]]
        if len(changed_ids) != len(set(changed_ids)):
            raise ValueError("Duplicate revision target")
        for r in decision["revisions"]:
            if r["belief_id"] not in focal or r["belief_id"] not in packets:
                raise ValueError("Revision target was not an inspected focal belief")
            if not r["statement"].strip() or not r["reasoning"].strip():
                raise ValueError("Empty statement or explanation")
            if not r["evidence_refs"] or not set(r["evidence_refs"]) <= allowed_refs:
                raise ValueError("Revision needs inspected evidence references")
            if not set(r["related_belief_ids"]) <= packets.keys():
                raise ValueError("Related belief was not inspected")
        decision_hash = digest({"decision": decision, "context": context})
        with sqlite3.connect(self.working) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT decision_hash FROM contextual_review_runs WHERE run_id=?", (run_id,)).fetchone()
            if prior:
                if prior[0] != decision_hash:
                    raise ValueError("Run ID reused with different input")
                return {"run_id": run_id, "replayed": True, "changed": len(changed_ids)}
            # Fence every belief used as context, not just the write targets.
            for belief_id, packet in packets.items():
                table = 'user_beliefs_archive' if packet.get('archived') else 'user_beliefs'
                current = conn.execute(f"SELECT * FROM {table} WHERE id=?", (belief_id,)).fetchone()
                if current is None or digest(dict(current)) != packet["version"]:
                    raise ValueError("Review context changed; investigate again")
                evidence_table = 'belief_evidence_archive' if packet.get('archived') else 'belief_evidence'
                evidence_now = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {evidence_table} WHERE belief_id=? ORDER BY source_date,id", (belief_id,))]
                evidence_then = [{k:v for k,v in e.items() if k not in ('ref','original_ref')} for e in packet['evidence']]
                if digest(evidence_now) != digest(evidence_then):
                    raise ValueError("Review evidence changed; investigate again")
            for r in decision["revisions"]:
                before = packets[r["belief_id"]]["belief"]
                if before.get("locked"):
                    raise ValueError("Owner-locked belief cannot be revised")
                conn.execute(
                    "UPDATE user_beliefs SET statement=?,confidence=?,status=?,conditions=?,updated_at=? WHERE id=?",
                    (r["statement"], r["confidence"], r["status"],
                     canonical({"text": r["conditions"]}) if r["conditions"] is not None else None,
                     datetime.now(timezone.utc).isoformat(), r["belief_id"]),
                )
                after = dict(conn.execute("SELECT * FROM user_beliefs WHERE id=?", (r["belief_id"],)).fetchone())
                conn.execute("INSERT INTO contextual_review_revisions VALUES (?,?,?,?,?)",
                             (run_id, r["belief_id"], canonical(before), canonical(after), canonical(r)))
            conn.execute("INSERT INTO contextual_review_runs VALUES (?,?,?,?,?)",
                         (run_id, decision_hash, canonical(decision), canonical(context), datetime.now(timezone.utc).isoformat()))
        return {"run_id": run_id, "replayed": False, "changed": len(changed_ids)}


def investigate(store: ReviewCopy, invoke: Callable, *, question: str, focal_ids: list[str],
                insights: list[dict] | None = None, max_rounds: int = 5) -> tuple[dict, dict, list[dict]]:
    """LLM selection over the entire current catalog, then expandable source review.

    No embedding cutoff or Python topic classifier. Bounded calls stop without writes
    if the model still needs context. Exact archived predecessor IDs are inspectable.
    """
    catalog = store.catalog()
    by_id = {b["id"]: b for b in catalog}
    if not focal_ids or not set(focal_ids) <= by_id.keys():
        raise ValueError("Unknown or empty focal belief set")
    context = {"question": question, "focal_belief_ids": focal_ids,
               "as_of_utc": datetime.now(timezone.utc).isoformat(),
               "inspected_beliefs": store.inspect(focal_ids), "source_days": [],
               "insights": insights or [], "search_history": [],
               "coverage": "Complete current belief catalog. Archived predecessors can be inspected by exact ID through legacy merge links, but there is no general archive semantic search. Evidence may have missing original source IDs."}
    transcript = []

    def search(query):
        result = invoke("belief_engine::context_search", {"query": query, "catalog": catalog})
        from app.assistant.agents.belief_engine.context_search.agent_form import AgentForm
        result = AgentForm.model_validate(result).model_dump()
        ids = [r["belief_id"] for r in result["candidates"]]
        if not set(ids) <= by_id.keys():
            raise ValueError("Search returned an unknown belief")
        context["search_history"].append({"query": query, "result": result})
        transcript.append({"agent": "search", "result": result})
        return ids

    selected = search({"question": question, "focal": [by_id[i] for i in focal_ids]})
    context["inspected_beliefs"] = store.inspect(list(dict.fromkeys(focal_ids + selected)))
    for round_index in range(max_rounds):
        context['review_round'] = round_index + 1
        context['remaining_rounds'] = max_rounds - round_index - 1
        index_references(context)
        result = invoke("belief_engine::context_review", context)
        from app.assistant.agents.belief_engine.context_review.agent_form import AgentForm
        result = AgentForm.model_validate(result).model_dump()
        transcript.append({"agent": "review", "result": result})
        if result["phase"] == "finish":
            if any(result[k] for k in ("search_queries", "inspect_belief_ids", "source_dates")):
                raise ValueError("Finished review still has requests")
            return result, context, transcript
        if result["revisions"]:
            raise ValueError("Investigation cannot also revise beliefs")
        ids = [p["belief"]["id"] for p in context["inspected_beliefs"]]
        ids.extend(result["inspect_belief_ids"])
        for query in result["search_queries"]:
            ids.extend(search(query))
        context["inspected_beliefs"] = store.inspect(list(dict.fromkeys(ids)))
        have_days = {d["date_utc"] for d in context["source_days"]}
        for day in result["source_dates"]:
            if day not in have_days:
                context["source_days"].append(store.source_day(day))
                have_days.add(day)
    raise RuntimeError("Investigation budget reached; no belief changes committed")


def commit_review(store: ReviewCopy, invoke: Callable, *, run_id: str, decision: dict,
                  context: dict, repair_attempts: int = 2) -> tuple[dict, dict, dict]:
    """Give invalid output back to the LLM; never guess replacement IDs or meanings."""
    context = dict(context)
    for attempt in range(repair_attempts + 1):
        try:
            receipt = store.apply(run_id, decision, context)
            return decision, context, receipt
        except ValueError as exc:
            if attempt == repair_attempts:
                raise
            context['validation_error'] = str(exc)
            context['previous_decision'] = decision
            context['remaining_rounds'] = 0
            decision = invoke('belief_engine::context_review', context)
    raise AssertionError('unreachable')


def standard_invoke(agent_name: str, payload: dict) -> dict:
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.scope.loader import load_scope
    from app.assistant.utils.pydantic_classes import Message
    scope = load_scope(
        {"tools": {"allowed_tools": []}, "resources": {"allowed_global_resources": []},
         "writes": {"write_unified_log": False, "write_kg": False, "allow_fact_extraction": False}},
        identity={"owner_id": "belief_review_copy", "actor_id": agent_name, "surface": "pipeline"},
    )
    agent = DI.agent_factory.create_agent(agent_name)
    if agent is None:
        raise RuntimeError(f"Missing agent: {agent_name}")
    response = agent.action_handler(Message(scope_context=scope, agent_input={"review_input": payload}))
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        raise RuntimeError("Invalid agent result")
    return data
