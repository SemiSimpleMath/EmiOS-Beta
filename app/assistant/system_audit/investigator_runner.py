"""system_audit.investigator_runner — the deep pass + the Claude handoff.

Runs system_audit::investigator (gpt-5.4) over each ASSEMBLED case's dossier,
records the read in the register (which arms regression detection), appends
the investigator sections to the dossier file, and moves the case to
awaiting_claude. A daily digest ticket tells the owner cases are waiting.

Repairs are never applied here — the pipeline ends at the inbox; repairs
happen in interactive Claude Code sessions (owner's in-the-loop rule).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_DIGEST_MARKER = Path("data/claude_audit_inbox/.last_digest")
_DIGEST_MIN_INTERVAL_S = 24 * 3600
_MAX_PER_RUN = 3


def run_investigations(limit: int = _MAX_PER_RUN) -> int:
    """Investigate up to `limit` assembled cases. Returns the number investigated."""
    from app.assistant.system_audit import case_store

    cases = case_store.list_cases(statuses=["assembled"], limit=limit)
    done = 0
    for case in cases:
        try:
            _investigate_one(case)
            done += 1
        except Exception as e:
            logger.error("[investigator] case %s failed: %s", case["id"], e, exc_info=True)
    return done


def _investigate_one(case: dict) -> None:
    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.scope.loader import load_scope_for_source
    from app.assistant.system_audit import case_store
    from app.assistant.utils.pydantic_classes import Message

    dossier_path = case.get("dossier_path")
    if not dossier_path or not Path(dossier_path).exists():
        raise FileNotFoundError(f"case {case['id']}: dossier missing at {dossier_path!r}")
    dossier = Path(dossier_path).read_text(encoding="utf-8")

    scope = load_scope_for_source(
        kind="subsystem", source_id="system_audit", actor_id="system_investigator",
        identity_overrides={"surface": "internal",
                            "scope_id": f"system_audit::investigator::{case['id']}"})
    agent = DI.agent_factory.create_agent("system_audit::investigator")
    if agent is None:
        raise RuntimeError("system_audit::investigator agent not found")
    result = agent.action_handler(Message(agent_input={"task": dossier}, scope_context=scope))
    data = getattr(result, "data", None)
    if not isinstance(data, dict) or not data.get("implicated_subsystem"):
        raise ValueError(f"case {case['id']}: investigator returned no usable read")

    repair_options = [r if isinstance(r, dict) else dict(r)
                      for r in (data.get("repair_options") or [])]
    status = case_store.mark_investigated(
        case["id"],
        preliminary_read=str(data.get("causal_chain") or ""),
        implicated_subsystem=str(data.get("implicated_subsystem")).strip().lower(),
        repair_suggestions=repair_options,
        confidence=float(data.get("confidence") or 0.0),
    )

    _append_read(Path(dossier_path), case["id"], data, status)
    case_store.transition(case["id"], "awaiting_claude")
    logger.info("[investigator] case %s -> awaiting_claude (subsystem=%s, %s)",
                case["id"], data.get("implicated_subsystem"), status)


def _append_read(path: Path, case_id: str, data: dict, status: str) -> None:
    lines = ["\n\n## Investigator read\n",
             f"**Summary:** {data.get('summary', '')}",
             f"**Implicated subsystem:** `{data.get('implicated_subsystem', '')}` "
             f"(confidence {float(data.get('confidence') or 0.0):.2f})"]
    if status == "regressed":
        lines.append("\n**REGRESSION:** this subsystem was already resolved in an earlier "
                     "case — the fix did not hold. Treat with priority.")
    lines.append("\n**Causal chain:**\n")
    lines.append(str(data.get("causal_chain") or ""))
    lines.append("\n**Repair options (suggestions only — nothing is auto-applied):**")
    for r in (data.get("repair_options") or []):
        rd = r if isinstance(r, dict) else dict(r)
        lines.append(f"- [{rd.get('level')}] {rd.get('description')}")
    lines.append("\n## What I need from Claude\n")
    lines.append("Verify the causal chain against the code, confirm or correct the "
                 "diagnosis, discuss the repair with the owner, and after the repair "
                 "ships write a `## Resolution` section here (diagnosis, commits, "
                 "disposition) and set `status: resolved` in the frontmatter.")
    text = path.read_text(encoding="utf-8").replace("status: assembled", "status: awaiting_claude", 1)
    path.write_text(text + "\n".join(lines), encoding="utf-8")


def maybe_digest() -> Optional[str]:
    """At most daily: one ticket telling the owner how many cases await Claude."""
    from app.assistant.system_audit import case_store

    waiting = case_store.list_cases(statuses=["awaiting_claude"], limit=20)
    if not waiting:
        return None
    if _DIGEST_MARKER.exists() and (time.time() - _DIGEST_MARKER.stat().st_mtime) < _DIGEST_MIN_INTERVAL_S:
        return None

    from app.assistant.ServiceLocator.service_locator import DI
    from app.assistant.ticket_manager import get_ticket_manager
    from app.assistant.utils.pydantic_classes import Message

    lines = [f"- {c['id']}: {c['summary']}" + (" [REGRESSION]" if c.get("recurrence_of") else "")
             for c in waiting]
    tm = get_ticket_manager()
    ticket = tm.create_ticket(
        ticket_type="system_audit", suggestion_type="system_audit_digest",
        title=f"{len(waiting)} audit case(s) ready for Claude",
        message=("The system auditor has cases waiting in the Claude inbox "
                 "(data/claude_audit_inbox/):\n" + "\n".join(lines)),
        trigger_context={"audit_case_ids": [c["id"] for c in waiting]},
        valid_hours=24,
    )
    if not ticket or not tm.mark_proposed(ticket.ticket_id):
        return None
    payload = ticket.to_dict()
    DI.event_hub.publish(Message(event_topic="proactive_suggestion", data=payload))
    _DIGEST_MARKER.parent.mkdir(parents=True, exist_ok=True)
    _DIGEST_MARKER.touch()
    logger.info("[investigator] digest ticket %s for %d case(s)", ticket.ticket_id, len(waiting))
    return ticket.ticket_id
