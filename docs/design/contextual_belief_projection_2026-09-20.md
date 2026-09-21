# Contextual belief projection

Implemented 2026-09-20 in the Dayflow routine stage.

## Flow

1. Read the active belief export, authoritative calendar, daily theme/status/milestones
   and dated weekly context.
2. `dayflow_belief_selector` reads the full catalog and selects potentially relevant exact
   keys using meaning and context. Kind/domain/tags do not gate admission. Python validates
   identity only; duplicate or invented keys cannot silently select another belief.
3. `dayflow_routine_writer` receives original selected statements and qualifiers. It places
   useful guidance into the supplied hourly scaffold, citing `[belief:belief_key]`.
   Both agents use standard AgentFactory invocation, agent forms and Jinja prompts.
4. Publish the existing routine resource; save selected keys and selection reasoning in its
   latest pointer. Beliefs and their observation dates are not edited by projection.

## Synthetic model examples

The same belief, `lesson.reading_glasses`, says the user often forgets reading glasses for
instrument lessons. It is filed as a health preference, demonstrating cross-domain relevance.

| Supplied context | Observed writer output |
| --- | --- |
| Lesson at 17:00 | One reminder in the 16:00 slot before the lesson, citing the belief. |
| Lesson moved to 12:00 | Preparation moved to 11:00; no standing 16:00 glasses rule. |
| Glasses packed; reminder already delivered | Suppressed repeat reminders and recorded resolution. |

A separate morning-only creamer preference was not generalized into an afternoon action.
An explicit evening lights instruction retained its clock time. These are actual outputs
from three synthetic model evaluations, not private user data or proof of general accuracy.

## Cache and operational limits

The fingerprint includes the date, full calendar, active catalog, day theme, current status,
milestones and rendered weekly context. Generated timestamps and clock passage alone do not
cause new model calls. Unchanged inputs reuse and trim the cached routine. Existing hourly
eligibility remains: context changes are reflected at the next eligible pipeline run, not
immediately on chat. Regeneration normally uses two model calls instead of one; ordinary
chat has no new calls. The selector reads the full catalog, so cost scales with its size.

Guidance does not prove execution. The orchestrator must consult current work/finalizer/reply
history before acting; the projection receives daily milestones, not every work receipt.
Acknowledgment is not completion. Missing confirmation does not justify repeated contact.

Selected records retain identity and qualifiers, but the selector does not retrieve original
chats or evidence histories. Full provenance investigation remains in the copy-only belief
review pilot. Weekly candidates retain their evidence/conditions as historical context;
proper weekly ingestion into belief reconciliation remains an open task (MEM15).

The nightly merger, support accounting and decay are unchanged (MEM16 remains open).
No live database mutation or application restart is part of this change.
