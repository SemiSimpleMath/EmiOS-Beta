# TODO — concern continuity: a decline should outlive the concern it was given about

**Status: OPTION A IMPLEMENTED 2026-09-24** (calendar anchors). Investigated 2026-09-19.
The bug that used to sit on top of this — the owner's words never reaching the concern at all
— was fixed separately; see "What already mitigates it" so the two are not confused.

## What shipped

A concern may now carry an **`anchor`**: the identity of the thing it is about, as opposed to
the identity of the noticing.

- **The context supplies real ids.** The calendar tool's human-readable `content` carries no
  event id (it is written for planners reading prose), while the ids ride on its `data_list`.
  `context_builder._with_calendar_anchors` prefixes each rendered event line with
  `[calendar:<event id>]`, rewriting the tool's own output rather than reimplementing its
  formatting, so location, attendees and description survive. The shared calendar tool is
  untouched.
- **The anchor is the per-INSTANCE id** (the fetch passes `single_events=True`), so this
  year's picture day and next year's are different anchors. A decline expires by construction
  instead of needing an expiry rule, which was an open question in the original write-up.
- **The form has an optional `anchor`**, described as a token to COPY and never to invent or
  paraphrase from a title. Null for patterns with no single underlying item.
- **`persist` enforces it.** `_settled_anchors` collects anchors carrying a standing ruling
  across every bucket, and a new concern whose anchor is already settled is refused. The
  attempt is journalled on the concern that ruled, with a `suppressed_remint_count`, so the
  suppression is auditable and the next tick's recently-closed section shows the worry came
  back rather than hiding it.

**A standing ruling is exactly two things:** an owner decline (`user_declined_at_utc`) or
`accept_chronic`. **`resolved` is deliberately excluded** — resolved means the need was met or
the moment passed, not refused, so monthly timesheets and the dogs' medication keep minting per
cycle. Suppressing those would have broken the feature to fix the bug.

15 tests in `app/assistant/tests/dayflow/test_concern_anchor_continuity.py`, including the real
picture-day case, the recurring-obligation case that must NOT be suppressed, and legacy
unanchored concerns.

### What is still open

- **Calendar anchors only.** Pods and chat clusters already appear in evidence as real ids
  (`datapod:chat_cluster:…`), so extending is mechanical, but it is not done. A concern
  anchored to a pod rather than an event gets no guard yet.
- **Every concern already in the register has no anchor**, so the guard applies to new
  concerns from here. Nothing backfills.
- **The model has to copy the token correctly.** A missed copy means no suppression, which is
  the safe direction. A copy of the wrong settled anchor would wrongly suppress one concern;
  the risk is small and the journal makes it visible.
- **Option B is unbuilt and may still be wanted** for recurring worries that anchor to
  nothing ("work stress has been recurring"). Those remain re-mintable.

## The problem in one line

A concern's identity is minted fresh every time the noticer notices something, so declining
one says nothing about the next one about the same thing.

## The mechanism

`persist.apply_noticer_output` dedups on `concern_id`, and only against the **active** and
**addressing** buckets. Every new concern gets a fresh UUID. So:

1. The noticer sees evidence and files concern A.
2. You decline it. A is journalled, stamped `user_declined_at_utc`, parked dormant.
3. Next tick, the evidence that produced A is *still in context* — the calendar event has not
   moved, the newsletter is still in the pod store.
4. The noticer re-derives the same worry and files it as concern B, under a new UUID.
5. B does not collide with A, because A is dormant and the dedup only looks at active and
   addressing, and because the id is new anyway.

Your decline attached to A. B never heard about it.

`context_builder._build_concerns_recently_closed` already states this diagnosis in its own
docstring, and cites a sleep concern re-minted five times, twice past an `accept_chronic`
that had deliberately archived it.

## Evidence from the live register (read 2026-09-24)

Picture day is the case that prompted this. Five concerns, same child, same event:

| date | concern | what happened |
|---|---|---|
| 09-14 | `3a9f2c71` | "South Lake makeup Picture Day needs clothing and packet preparation" |
| 09-17 | `3a9f2c71` | **declined**, journalled with the owner's words, parked dormant |
| 09-18 | `3e9b1f52` | **new concern, same event**: "makeup Picture Day is today and prep details need to be confirmed" |
| 09-19 | `3e9b1f52` | owner declines again; the event has passed anyway |

Ask-A-Scientist Night shows the same shape inside two days: three concerns (09-12, 09-13,
09-13), one of them carrying a decline from 09-14.

Four concerns in the register currently carry a `user_declined_at_utc`. Three are dormant,
one resolved.

**A caution on counting.** A crude title-similarity sweep reports 14 near-duplicate clusters,
but that number is inflated and should not be quoted. Several are false positives: three
different family members' birthdays share the phrasing "<name>'s birthday is approaching and
prep may be needed" and are three genuinely different concerns. Others are legitimately
recurring — monthly timesheets and the dogs' flea medication mint per cycle, which is
arguably correct. The real signal is narrower: **several concerns about ONE event, minted
days apart.**

## What already mitigates it, and why that is not enough

Two things landed that help, and the second one is why this is no longer urgent:

- **`concerns_recently_closed`** (commit `4b433549`, 2026-09-16) shows the noticer the
  decisions it already made — recently resolved concerns plus every dormant one — so it does
  not decide blind.
- **The reply field fix** (landed in the last week) means a decline now actually reaches the
  concern with the owner's words attached, which is what stamps `user_declined_at_utc` and
  parks it dormant in the first place. Before that, a decline was indistinguishable from a
  system drop, so the picture day concern was never parked and `concerns_recently_closed` had
  nothing useful to show.

Both are real improvements. Since 09-18 only four concerns have been minted and three look
genuinely new, so the observed rate is now low.

But the defence is **prompt-level**: the register is shown to a model, and the model is
trusted not to re-derive. That is not a structural guarantee. It demonstrably failed at least
once — the picture day twin on 09-18 was minted *after* the recently-closed section shipped
on 09-16 — and the sleep concern in the docstring failed five times.

## Design options

**A. Identity from the thing noticed, not from the noticing.** Most of these concerns are
anchored to something with a stable id already sitting in their `evidence`: a calendar event,
a pod id, a KG entity. If a concern declared an anchor, dedup could ask "is there already a
concern for this anchor, in any bucket including dormant" instead of comparing UUIDs. This is
the option that matches the house rule about identity coming from ids and deterministic joins
rather than wording. Cost: something has to choose the anchor at mint time, and not every
concern has a clean one (a pattern like "work stress has been recurring" anchors to nothing).

**B. A decline that covers a subject plus a window.** Rather than binding the decline to one
concern, record "the owner declined anything about that child's picture day until it passes".
Cheaper than A and directly matches how people actually mean it: the owner said "stop this
task and everything related to it", which was never about one UUID. Cost: needs a subject and
an expiry, and both are judgement calls.

**C. Make the noticer's own dedup structural.** Keep concern ids as they are, but have the
noticer's persist step refuse a new concern whose anchor or subject matches a dormant one
carrying `user_declined_at_utc`, unless something material changed. Cost: "something material
changed" is exactly the judgement that is hard to make deterministic, and a wrong refusal
silences a real concern — the failure mode `apply_work_outcome` already goes out of its way to
avoid.

**Recommendation: A, with B as the fallback when no anchor exists.** A is the only one that
puts identity on the thing in the world rather than on a phrasing or a UUID, which is the
rule this codebase already applies elsewhere. B covers the "recurring worry with no object"
cases that A cannot reach.

## Open questions

- **Where does the anchor come from?** The noticer emits `evidence` entries that already carry
  `kind` and `ref` (`calendar_event`, `chat_msg`, pod ids). Is the first evidence ref a good
  enough anchor, or does the agent need to name one explicitly?
- **Does a decline expire?** "Not this picture day" should not silence next year's. An anchor
  tied to a dated calendar event expires naturally; a subject-level decline needs a rule.
- **What about the legitimate recurrences?** Timesheets and flea medication SHOULD mint again
  each cycle. Any dedup has to let those through, which argues for anchoring on the specific
  event instance rather than the subject.
- **Is `accept_chronic` the same mechanism?** It parks a concern as real-but-not-worth-ticking.
  The docstring says one concern was re-minted past it twice, so whatever fixes declines
  probably has to cover chronic acceptance too.

## How you would know it worked

The register stops accumulating several concerns about one event minted days apart. A concrete
regression test: decline a concern anchored to a dated calendar event, run a noticer tick with
that event still in context, and assert no second concern appears for the same anchor.

## Related

- `docs/design/todo_emergency_trigger_routine.md` — the other open design item from this pass.
- `docs/design/bug_list_2026-09-18.md` — where the reply-field bug was tracked before it was
  fixed.
