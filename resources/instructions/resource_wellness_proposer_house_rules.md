# Wellness proposer house rules — Jukka's household

Household-specific calibration for wellness_proposer: equipment
available, time windows, recurring routines, dietary/health constraints
relevant to training, recovery preferences, novel things worth trying.

Mirrors meal_proposer_house_rules + noticer_house_rules pattern —
separable from the agent's stable scaffold so skill_distiller can
append learned patterns here over time.

---

## Equipment available

(Fill in as you confirm what's at home — the proposer references this
for equipment_used and won't propose anything not on the list.)

- Road bike (outdoor)
- (TBD) — add as discovered: indoor trainer? treadmill? rowing machine?
  dumbbells? kettlebells? yoga mat? resistance bands? pull-up bar?

## Time windows that work

(Constraints that shape proposed_start_local.)

- Jukka works from home, full remote. Morning (6-8am) and early evening
  (5-7pm) are the natural workout windows.
- Lunch break (12-1pm) is short — only good for a quick walk or mobility
  break, not a full workout.
- Friday Night Meats is the family tradition; don't propose a hard
  workout for Friday evening.

## Sleep target + bedtime preferences

- Target sleep: 7-8 hours
- Default bedtime: 10:30pm-ish — wind-down should start ~9:30pm
- IF (intermittent fasting) — usually skips breakfast, but this means
  morning workouts should be EASY/MODERATE, not hard fasted sessions
  unless explicitly trained for that

## Recurring wellness routines (proposer should preserve these)

- (TBD — fill in as patterns become clear)
  Examples to consider:
  - Tuesday morning bike ride?
  - Saturday family walk?
  - Sunday recovery / mobility day?

## Health constraints relevant to training

- Jukka has GERD — avoid hard cardio within 90 min of meals
- Jukka is prediabetic — moderate cardio after dinner is beneficial
- No known joint issues currently — confirm before proposing impact
  activity (running on pavement, plyometrics)
- Heat sensitivity: California sun gets brutal — outdoor cardio in
  summer should be morning only, with hydration proposal alongside

## Recovery preferences

- After a hard session, recovery the next day is a LIGHT WALK or yoga
  — not "nothing." Movement aids recovery.
- After 2 short sleep nights in a row, the next day is rest_day_advisory.
- Sleep < 6h average for last 3 nights → MANDATORY recovery; do not
  propose any moderate or hard intensity.

## Worked example — proposal addressing the Jukka-fatigue concern

If addressable_concerns contains "Jukka's sleep/fatigue has been
recurring", a proposal addressing it might look like:

```
kind: "sleep_routine"
actors: ["Jukka"]
date: "2026-05-20"
proposed_start_local: "2026-05-20T21:30:00-07:00"
duration_minutes: 30
summary: "Wind down 9:30pm: screens off, dim lights, read."
source: "concern_addressing"
confidence: "high"
novelty: "familiar"
reasoning: "Fatigue concern + late screen exposure pattern. Sleep is the
  dominant lever; a wind-down routine before the 10:30 bedtime helps."
```

Paired with a movement proposal for the same day:

```
kind: "workout"
actors: ["Jukka"]
date: "2026-05-20"
workout_type: "walk"
intensity: "low"
duration_minutes: 30
summary: "Easy 30 min outdoor walk after lunch."
source: "concern_addressing"
confidence: "high"
novelty: "familiar"
equipment_used: []
reasoning: "Light movement supports recovery without adding load.
  Outdoor + daylight also helps the sleep concern via circadian effect."
```

Notice:
- Multiple proposals can address ONE concern from different angles
  (sleep routine + light walk).
- Intensity stays low because the concern signals depleted state.
- Reasoning explicitly ties to the concern.

## Family-roster awareness

- When Katy is home and willing, a family bike ride or walk is high-
  value novelty (the kids benefit from active outdoor time too).
- When Jukka is solo (family away), workout windows expand — propose
  longer or more focused sessions.
- Annika has been skipping breakfast — flag as a meal_proposer concern
  in free_form_thinking; not a wellness_proposer action.

## Novel things worth trying

(TBD — add as Jukka mentions interest. Examples to consider:
- Yoga class at local studio?
- Group fitness?
- Specific running goals (5K, 10K)?
- Cycling event signup?
- Strength program (StrongLifts, Starting Strength, etc.)?)

When proposing something novel, set novelty="novel" and provide a
novelty_rationale explaining why now (concern + opportunity, not just
random).

## Out of scope — always escalate, never propose

- Doctor / dentist / specialist appointments
- Medication adjustments
- Symptom investigation
- Anything requiring medical judgment

If addressable_concerns mention anything in this list, raise it in
free_form_thinking and address NOTHING in proposals related to it.
