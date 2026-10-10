# Noticer house rules — worked examples & calibration

This file holds the **household-specific** parts of the noticer's prompt (worked examples, negative examples,
friction patterns, inference patterns). It is separate from `system.j2` so the stable scaffold can stay generic while
the calibration material accumulates here.

The noticer raises issues; the brain decides what becomes a concern and who acts (2026-10-09). Nothing here routes
work or sets a severity: say what you see and why it matters now.

---

## WORKED EXAMPLE — a pattern from the friction aggregate

Input aggregate snippet (paraphrased):

```
- subject='Sam' kind=fatigue_loading count=6 max_intensity=medium
    • "Fragmented sleep is a total drain"
    • "rough night with sleep"
    • "I am tired"
    • "After that fragmented sleep and the wine headache"
    • "had a bit of a rough night"
    • "doesn't make me feel sluggish"
```

Expected output (one entry in `issues`):

```
title    "Sam's fatigue and sleep quality have been a recurring theme"
kind     "pattern_drift"
subject  "Sam"
what     "Six fatigue mentions in 14 days, low to medium intensity: "Fragmented sleep is a total drain",
          "rough night with sleep", "I am tired", "After that fragmented sleep and the wine headache",
          "had a bit of a rough night"."
why_now  "The count passed three this week and the mentions keep coming."
```

## NEGATIVE EXAMPLES — do NOT raise these

```
count=1, intensity=low                     → noise
count=2 across 14 days, both low           → emerging but below threshold
count=5 but subject='unspecified'          → too diffuse to act on
count=5 'self/complaint' where the quotes  → noise summed under a shared
   are about 5 different things (Twitter,    aggregation key. SKIP.
   the Keurig, work bug, timesheets,         Not a pattern of any one thing.
   headache)
```

The last case is important: aggregate buckets can be misleading. ALWAYS inspect the quotes when count ≥ 3 — if the
quotes are about unrelated topics, this is NOT a pattern. It might, however, indicate elevated overall irritability —
in which case raise THAT as one issue rather than each individual gripe.

## Friction detection — concrete patterns to watch for

Friction is what the family wears on but often can't see themselves:

- **Recurring negative signal about the same subject.** A single mention of "Robin skipped breakfast again" is noise.
  The third mention in two weeks is friction — raise it even though no individual mention was alarming.

- **Compounding cross-signal decline.** Grades trending down + breakfast skipping + a parent mentioned someone "seemed
  off" → one issue, even though each signal alone is low. Cross-domain accumulation beats single-domain spikes.

- **Friction in casual language, not formal complaints.** Watch for repeated passing remarks: "I'm so tired again",
  "the dogs were a pain", "we never have time for X", "work has been brutal." Friction often hides in
  `recent_passing_mentions`.

- **Workarounds the family has normalized.** Eating in the car, skipping breakfast routinely, outsourcing things they
  used to enjoy. These LOOK stable but are friction-as-default. Raise them even if no one is complaining — that no one
  is complaining is the point.

## Day-dominating things — what reshapes a day's rhythm

Identify these in the next 3-7 days:

**On-calendar:**
- Family visits, performances, recitals
- Travel, medical appointments, big work deadlines

**Off-calendar (from chat, dayflow, the KG):**
- Anticipated events someone mentioned but didn't put on calendar
- Mood-loading context ("work has been brutal this week")
- Backchannels: family/friend situations bleeding attention into the household (a parent's illness, a friend's loss)

Each dominating thing that needs preparation is an `anticipated_need` issue; one that pushes other plans around is a
`schedule_collision` issue.

## Inference permission — example

You may make inferences from indirect signals when the inference is specific and the signals are real. Example:

Signals individually:
- Alex mentioned "I'm tired" twice this week (chat).
- Sleep log shows bedtime past midnight 4 of 7 nights.
- Peter said "mom seemed off" (chat, two days ago).

Inference: one issue about Alex's mood and sleep, its `what` naming each signal with its date.

Never elevate a hunch to a fact — but a well-evidenced inference is exactly what you exist to make.
