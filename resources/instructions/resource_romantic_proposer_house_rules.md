# Romantic proposer house rules — Jukka + Katy

Household-specific calibration for romantic_proposer: key dates, what
Katy likes, restaurants worth proposing, babysitter logistics, what
NOT to suggest.

Mirrors meal_proposer / wellness_proposer house_rules pattern —
separable from the agent's stable scaffold so skill_distiller can
append learned patterns over time.

---

## Key dates (fill in as you confirm them)

- Anniversary: TBD — add as `anniversary_date: YYYY-MM-DD` to
  `resources/user/resource_user_data.json` so the context builder picks
  it up automatically.
- Katy's birthday: 1976-08-18 (already in resource_user_data.json)
- Jukka's birthday: 1975-03-18 (already in resource_user_data.json)
- Peter's birthday: 2010-06-02
- Annika's birthday: 2013-08-12

For now, the proposer reads birthdays from resource_user_data.json
automatically via `key_dates`. Anniversary needs the field added there.

## What Katy likes (calibration — fill in over time)

- (TBD — examples to consider as patterns emerge:
  - Favorite restaurant(s)?
  - Coffee order?
  - Flowers she actually likes (vs ones she finds wasteful)?
  - Books / shows she's wanted to watch together?
  - Walks where? Beach / hike / neighborhood?
  - What kind of small gestures land vs. fall flat?)

## What NOT to propose

- Sexual content — out of scope; the proposer's prompt enforces this.
- "Talk about" / "reconnect" / "discuss feelings" — these land creepy.
  Concrete actions only.
- Anything that bumps Friday Night Meats — that's family-anchored.
- Surprises that require Katy to suddenly clear her schedule.
- Anything making Katy do extra work she didn't ask for (e.g., asking
  her to plan a date night).

## Babysitter logistics

- (TBD — add who babysits, lead time, typical cost)
- Default assumption for cost_estimate: $25-40 for a 2-3 hour evening
  date if external sitter; $0 if family/neighbor.

## Worked example — small_gesture addressing "Katy had a hard week"

```
kind: "small_gesture"
actors: ["Jukka"]
date: "2026-05-20"
proposed_start_local: null
duration_minutes: null
summary: "Pick up Katy's favorite coffee on the way back from the morning ride."
source: "concern_addressing"
confidence: "high"
novelty: "familiar"
cost_estimate_usd: 6
requires_babysitter: false
advance_required_days: 0
addresses_concern_ids: ["<the Katy-stress concern's id>"]
reasoning: "Concern: Katy had a tough week. Small, low-friction gesture
  that fits a routine moment (morning ride return). Familiar = signals
  attentiveness without performance."
```

Notice:
- Actor is Jukka (not Jukka + Katy) — this is HIM doing something FOR her.
- Same-day, no babysitter, no advance prep — high-confidence small.
- Cost is real ($6 coffee), not aspirational.
- Reasoning ties to the concern directly.

## Worked example — anniversary_prep, 3 weeks out

```
kind: "anniversary_prep"
actors: ["Jukka"]
date: "2026-06-01"   # 3 weeks before anniversary
proposed_start_local: null
duration_minutes: null
summary: "Book a 2-night sitter for anniversary weekend + reserve dinner at <place>."
source: "anniversary_prep"
confidence: "high"
novelty: "familiar"
cost_estimate_usd: 250
requires_babysitter: true
advance_required_days: 14
addresses_concern_ids: []
reasoning: "Anniversary in 3 weeks. Restaurants book up — secure the
  reservation now. Concrete next step Jukka takes today, not a vague
  'plan something.'"
```

## Calibration for confidence + novelty

- **High confidence + familiar**: routine maintenance, established
  recipes. "Pick up coffee on the way home." Most proposals should be here.
- **Medium confidence**: a date night out at a known restaurant on a day
  that fits the calendar.
- **Low confidence**: anything based on inferred mood from a single
  signal. Should rarely be a proposal at all — bias toward skipping.
- **Novel**: trying a new restaurant or activity. Requires
  novelty_rationale (why now). Cap ~1 per month.

## Family-roster awareness

- If Katy is traveling for work — propose a small_gesture for her
  return day. Skip date_night_out.
- If Jukka is solo with kids — skip date_night_out entirely. Maybe a
  small_gesture (flowers for when she's back).
- If kids are at a sleepover / grandparent — that's a natural
  date_night_in window. Propose accordingly.

## Out of scope — defer to chat_brain or just don't surface

- Conflict / argument mediation. If concerns surface tension, raise it
  in free_form_thinking — don't try to propose around it.
- Date-night-vs-other-event conflicts. Phase 3's conflict detector is
  a separate component (not yet built).
