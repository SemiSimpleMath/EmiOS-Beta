# Scheduler arbiter priority rules — Jukka's household

Guidance for `scheduler_arbiter` when resolving conflicts between
intention.* proposals across meal / wellness / romantic domains.
These are SOFT rules — the arbiter combines them with context-specific
judgment. When a rule fits cleanly, follow it. When context says
otherwise, override and explain in the resolution reasoning.

skill_distiller will eventually grow this file with learned household
preferences (e.g., "Jukka actually prefers X over Y when Z" — see the
TODO in `memory/todo.md`). For now, this is the starting calibration.

---

## Hard precedences (rarely overridden)

Anchored slots near these dates almost always win. If displacing one,
you must list a specific reason in your reasoning.

1. **Anniversary** (+/- 3-day window) > anything else in that window
2. **Family member birthday** (Peter, Annika, Katy, Jukka — +/- 1 day) > leisure intentions
3. **Pre-existing household calendar event** (Google Calendar) > any new intention
4. **Medical / health-urgent** > all leisure
5. **Friday Night Meats** > date_night_out on a Friday (family-anchored
   tradition; date night picks a different night)

## Soft precedences (most common cases)

These apply in the common no-anchor case. Use as defaults.

1. **Date night (out)** > flex family dinner — date_night involves
   reservation + babysitter coordination; family dinner replans easily.
2. **Anchor meal (USE-SOON food)** > flex meal on same day — perishable
   food drives the slot.
3. **Date night (in)** > flex weekend slot — small commitment to the
   relationship that won't recur this week if displaced.
4. **Hard workout (intensity matters)** > flex meal time — meals
   reshuffle around training windows.
5. **anniversary_prep** (lead-up to upcoming anniversary/birthday) >
   small_gesture — the prep has a deadline; gesture is opportunistic.
6. **Routine maintenance gesture** (small_gesture, hydration, walk)
   loses to anything with a fixed time slot.

## Tiebreakers when soft rules don't decide

In order, apply:

1. **Higher cost / lower frequency wins** — date_night_out > home meal;
   trip > date_night_in. Rare events deserve protection.
2. **Higher advance_required_days wins** — if proposer A planned 14
   days out and proposer B is same-day, A wins (planning was a real
   commitment).
3. **Higher confidence wins** — high-confidence proposal > medium >
   low. Speculative proposals don't displace confident ones.
4. **Specific actor count wins** — proposal naming both Jukka and Katy
   wins over solo proposals when both are home.

## When to ASK THE USER (punt to ConflictForUser)

Don't auto-resolve. Surface as a ticket.

- Two anchored intentions clash with comparable weight (e.g., both
  rooted in key_dates).
- Cost differential > $100 (date_night_out vs trip; expensive choice).
- Health/medical is one side of the conflict.
- Family-wide schedule change (e.g., Katy traveling AND a romantic
  proposal AND a wellness anchor all on the same day).
- Anything where the priority rules give contradictory guidance.
- A proposer asked the user via a concern that's still unresolved.

For each user-facing conflict, present 2-4 concrete options. Bad:
"discuss this." Good: "(a) Date night Wed, salmon moves to Tues. (b)
Salmon Wed, date night Fri after Friday Night Meats. (c) Skip the
date night this week — busy work week."

## Domain-specific notes

### Meal
- "anchor" slots from weekly_meal_planner (USE-SOON, family tradition)
  carry weight. Flex slots are infinitely movable.
- Family dinners with all 4 family members > 2-person meals when
  conflict touches family time.

### Wellness
- Recovery / sleep_routine intentions during a fatigue concern week
  ALMOST NEVER LOSE — they're the household's response to a real signal.
- Hydration / mobility breaks NEVER conflict (no time slot).

### Romantic
- Trip > date_night > anniversary_prep > small_gesture > quality_time
  in terms of "do not displace casually."
- Small_gesture / quality_time are forgiving — easy to defer one day.
- Date_night_out's `advance_required_days >= 1` is real — if you
  displace one with <24h to go, the booking is already in motion.

## When in doubt

Pick the option that:
1. Preserves a real commitment (booked, scheduled, key-date-anchored).
2. Doesn't ask Katy to suddenly clear her plans.
3. Is reversible / cheap to redo if wrong.
4. Has the lower regret cost ("if we skip this, can we do it next
   week?" — if yes, easier to skip).
