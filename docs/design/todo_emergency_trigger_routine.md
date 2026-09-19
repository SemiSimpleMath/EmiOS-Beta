# TODO — an emergency trigger routine

**Status: NOT BUILT. Owner's idea, 2026-09-18, recorded before the details fade.**
Nothing in this document is implemented. The interim measure that IS implemented is in the
last section, so nobody mistakes one for the other.

## The idea, in the owner's words

> Maybe we need an emergency trigger routine. This routine just gets all the current info
> and the info of whatever triggered it. Then a pre-programmed series of events occurs.

So: one entry point for "something is wrong", fed by any trigger, which gathers the
situation and then runs a FIXED sequence rather than asking a model what to do.

## Why a routine and not an agent decision

The reflex has to be the same every time, and it has to work when the expensive parts of
the system are the thing that is broken. An LLM deciding what to do in an emergency is a
call that can be slow, rate-limited, refused, or wrong, and a deterministic ladder cannot
be any of those. The model's place is in *describing* the situation, not in choosing
whether to react to it.

That does not mean no model is involved. The camera analyzer already classifies the frame,
and classification is exactly the judgment a model is good at. The line is: the model says
WHAT this is, the routine decides what HAPPENS.

## What it would need

**Triggers, more than one.** The obvious first is a camera frame classified `emergency`.
Others that plausibly belong on the same entry point: the bedroom camera's existing
`bedroom_emergency_alarm` post-handler, a smoke or water sensor if one is ever wired, an
explicit "this is an emergency" from the owner in chat, and possibly a health signal.
Each trigger should hand over a small typed record rather than call the ladder directly,
so the ladder has one shape of input to reason about.

**Situation gathering, bounded.** "All the current info" has to be capped, because an
emergency is the worst moment to be assembling a large prompt or waiting on a slow read.
Candidates, cheapest first: who is home and awake (the presence and AFK signals), the time
of day, the most recent frames from the other cameras, anything already open in the work
graph, and the last few minutes of chat. The bound matters more than the completeness.

**A ladder, with escalation over time rather than at once.** The current escalation policy
fires every configured surface simultaneously. An emergency wants steps that continue if
the earlier ones go unanswered: a spoken alert and an on-screen ticket now, a phone-reaching
channel if nothing is acknowledged within some window, and only then anything irreversible.
Each rung needs an explicit acknowledgement check, which the ticket system can already
answer — a ticket carries `user_action` and `responded_at`.

**An off switch that works.** Acknowledging must stop the ladder, and there must be a way to
stop it that does not depend on the part of the system that might be wedged.

## Design questions worth settling before any code

- **Does the ladder own the notification, or does it delegate?** The ticket tool, the chat
  publisher and the TTS path all exist. The routine probably sequences them rather than
  reimplementing them.
- **Where does the state live?** A ladder that survives a process restart needs its position
  written down. The work graph is the obvious substrate and is already the thing the
  scheduler wakes from, which makes the per-rung delay a wake primitive rather than a
  `sleep`.
- **What is genuinely irreversible, and is any of it in scope?** Unlocking or locking
  something, calling somebody, sounding an alarm. That list should be explicit and short,
  and it may be empty for v1.
- **How does it get tested?** A ladder nobody can dry-run is a ladder nobody will trust. A
  dry-run mode that logs the rungs it would have fired, without firing them, is probably a
  requirement rather than a nicety.

## What exists today that this would build on

- `camera_dispatcher._fire_escalation_surfaces` — reads
  `escalation_policy.per_category` and fires each named surface. Two surfaces exist:
  `chat_alert` and `dayflow_ticket`. It fires them all at once and has no notion of a
  sequence or of acknowledgement.
- `camera_post_handlers.py` — already holds `bedroom_emergency_alarm`, i.e. there is
  precedent for a named reflex hanging off a camera.
- `create_dayflow_ticket` — can now surface without blocking (`wait: false`), which is what
  makes a multi-rung ladder possible at all. Before that a rung would have held its thread.
- The work graph and `DayflowScheduler`'s per-node time wakes — the natural way to express
  "if this is not acknowledged in N minutes, do the next thing".

## The interim measure that IS implemented (2026-09-18)

Not the routine above. This is the small reasonable thing done in its place, so an
emergency is handled while the real design waits.

On a front-door frame classified `emergency`:

1. **Chat alert**, unchanged, immediate. This always worked.
2. **Dayflow ticket**, immediate, on screen and spoken. This was **broken** until
   2026-09-18: it asked for `ticket_kind: "info"`, which the tool rejects before creating
   anything, so no emergency ticket had ever been creatable. It now passes `notify` and
   `wait: false`, so it surfaces at once and lives out its full four hours instead of being
   withdrawn after ten minutes by its own wait timing out.
3. **The frame's pod is minted as `ring_doorbell_significant`** instead of the everyday
   `ring_doorbell_event`, via the new `pod_policy.source_kind_by_category`. That is the kind
   the dayflow orchestrator's ingestion allowlist already asks for, so the emergency now
   enters dayflow intake, gets triaged, and the orchestrator can reason about it and act.
   Everyday categories keep the everyday kind and stay out of dayflow's working set.

So the immediate alerting is deterministic and instant, and the follow-up thinking is
delegated to the orchestrator. What is missing, and what the routine above is for, is the
part that keeps escalating when nobody answers.

**Known and deliberately not fixed:** the Downstairs camera mints
`ring_downstairs_notable`, which is in no allowlist, so its pods never reach dayflow
either. Left alone because changing what dayflow ingests from a second camera is a
behaviour decision, not a bug fix.
