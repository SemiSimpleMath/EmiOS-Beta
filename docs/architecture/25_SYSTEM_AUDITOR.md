# System Auditor — learning from the system's own failures

The loop that turns user friction ("this is wrong", "why am I being asked
again", "your question makes no sense") into id-bound audit cases, evidence
dossiers, and repair proposals — with the repair itself always executed by a
human-supervised Claude Code session, never autonomously.

Shipped 2026-08-08. Design dialog + plan: `scratch/SYSTEM_AUDITOR_PLAN.md`
(gitignored). Package: `app/assistant/system_audit/`.

## The loop

```
user friction in chat ──► audit_signal_service (gut subscriber #3;
                          system_audit::friction_classifier, luna,
                          user msgs in master_room + slack)
                                   │ opens/attaches
                                   ▼
                     system_audit_case register (SQLite)
                     id-bound; dedup = ID JOINS, never wording;
                     lifecycle open → assembled → investigated →
                     awaiting_claude → resolved | dismissed (+ regressed)
                                   │ hourly (situation_audit drives it)
                                   ▼
                     evidence.assemble — deterministic forensics:
                     friction verbatim, chat window (±30 min, declared),
                     tickets + work graphs by id join (with id harvesting),
                     log excerpt (±5 min, declared filter/cap)
                                   ▼
                     system_audit::investigator (gpt-5.4):
                     causal chain w/ evidence refs, ONE subsystem slug,
                     repair options (prompt|config|code), needs_claude
                                   ▼
                     data/claude_audit_inbox/case_<id>.md
                     + daily digest ticket to the owner
                                   ▼
                     Claude Code session (interactive): verify, discuss
                     with the owner, repair, write ## Resolution
                                   ▼
                     inbox.ingest() → case resolved (commit refs harvested)
                     → future same-subsystem case = REGRESSED (loud)
```

## Components

| Piece | File | Notes |
|---|---|---|
| Friction classifier | `agents/system_audit/friction_classifier/` | luna; meta-feedback only — venting about life/others is not friction; confidence floor 0.6 |
| Friction ear | `system_audit/signal_service.py` | third gut subscriber; gated by `subsystems.yaml: system_audit`; crashes contained but loud |
| Register | `database/system_audit_case.py` + `system_audit/case_store.py` | id-overlap dedup ATTACHES to live cases; `mark_investigated` arms regression via subsystem join against resolved cases |
| Evidence assembler | `system_audit/evidence.py` | no LLM; windows/filters DECLARED in the dossier, full sources referenced by path |
| Investigator | `agents/system_audit/investigator/` + `system_audit/investigator_runner.py` | gpt-5.4; ≤3 cases/run; digest ticket ≤1/24h |
| Inbox / resolution | `system_audit/inbox.py` | Claude edits frontmatter `status:` + appends `## Resolution`; ingest folds it back |
| L0 auditor | `situation_auditor` (+ `situation_audit_runner.py`) | hourly; snapshot now includes friction + open cases; findings with ids enter the register (id-less findings are not cased — no dedup join); chat ping only on red health; the hourly run drives assemble → investigate → digest → ingest |

## Doctrine

- **Identity is ids.** Case dedup, ticket/work joins, and regression detection
  are deterministic joins; the LLMs judge, they never link.
- **Sense wide, spend narrow.** Always-on: one small luna call per user chat
  message in watched rooms. Premium reasoning only per opened case.
- **Repairs are never autonomous.** The pipeline's terminal output is a
  dossier and a digest ticket. The owner and Claude decide; Claude verifies
  the investigator's hypothesis against the code before any repair
  (the investigator is a junior diagnostician, not an authority).
- **Nothing evaporates.** Friction and findings either attach or open; every
  case reaches a terminal state; a fix that doesn't hold surfaces as
  REGRESSED instead of being re-diagnosed from scratch.

## Cross-references

- [SUBCONSCIOUS.md](SUBCONSCIOUS.md) — the sibling register pattern for the
  user's life (concerns); this page is the same shape pointed at the system.
- [22_KG_HEALTH_COMPONENTS.md](22_KG_HEALTH_COMPONENTS.md) — the KG's
  findings → investigator → executor loop this deliberately mirrors (with the
  executor replaced by human+Claude).
- [05_DAYFLOW.md](05_DAYFLOW.md) / [08_WORK_OBJECTS.md](08_WORK_OBJECTS.md) —
  the id chains the evidence assembler walks.
