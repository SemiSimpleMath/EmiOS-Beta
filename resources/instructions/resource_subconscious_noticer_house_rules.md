# Noticer house rules — worked examples & calibration

This file holds the **household-specific** parts of the noticer's prompt
(worked examples, negative examples, routing tables, friction bullets,
inference patterns). It's separate from `system.j2` so the stable scaffold
(role, 5-stage flow, hard rules, output contract) can stay generic while
the calibration material accumulates here over time. Eventually the
skill_distiller will append to this file automatically.

---

## WORKED EXAMPLE — pattern_drift from friction aggregate

Input aggregate snippet (paraphrased):

```
- subject='Jukka' kind=fatigue_loading count=6 max_intensity=medium
    • "Fragmented sleep is a total drain"
    • "rough night with sleep"
    • "I am tired"
    • "After that fragmented sleep and the wine headache"
    • "had a bit of a rough night"
    • "doesn't make me feel sluggish"
```

Expected output (one entry in `new_concerns`):

```
title         "Jukka's fatigue / sleep quality has been a recurring theme"
subject       "Jukka"
kind          "pattern_drift"
domain_tags   ["sleep", "fatigue", "health"]
severity      "medium"
horizon       "this_week"
evidence      [{kind:"pod", ref:"<pod_id>", snippet:"Fragmented sleep..."}, ...]
addressable_by ["meal_proposer", "wellness_proposer", "chat_brain"]
notes         "Six fatigue mentions across 14 days, intensities low-medium.
               Worth proposer attention: lighter/earlier dinners, exercise
               timing review, possible CPAP usage check."
```

## NEGATIVE EXAMPLES — do NOT flag these

```
count=1, intensity=low                     → noise, log only
count=2 across 14 days, both low           → emerging but below threshold
count=5 but subject='unspecified'          → too diffuse to act on
count=5 'self/complaint' where the quotes  → noise summed under a shared
   are about 5 different things (Twitter,    aggregation key. SKIP.
   the Keurig, work bug, timesheets,         Not a pattern of any one thing.
   headache)
```

The last case is important: aggregate buckets can be misleading.
ALWAYS inspect the quotes when count ≥ 3 — if the quotes are about
unrelated topics, this is NOT a pattern. It might, however, indicate
elevated overall irritability — in which case flag THAT as a separate
concern (e.g., a fatigue_loading concern referencing the irritability
volume as evidence) rather than each individual gripe.

## addressable_by routing for friction-derived pattern_drift

| Friction kind on subject                          | Route to                                                       |
|---------------------------------------------------|----------------------------------------------------------------|
| fatigue_loading / observed_decline about a person | [meal_proposer, wellness_proposer, chat_brain]                 |
| missed_routine about food/eating                  | [meal_proposer, chat_brain]                                    |
| missed_routine about exercise                     | [wellness_proposer, chat_brain]                                |
| complaint about household objects / tools         | [chat_brain]                                                   |
| tension between Jukka and Katy specifically       | [romantic_proposer, chat_brain]                                |
| tension between household members (general)       | [chat_brain, family_proposer]                                  |
| Katy stress / hard week mention                   | [romantic_proposer, chat_brain]                                |
| quality time drift (last date night was >Nweeks)  | [romantic_proposer]                                            |
| complaint about work                              | [chat_brain]                                                   |

## Other addressable_by examples

| Concern                                | Route to                                                  |
|----------------------------------------|-----------------------------------------------------------|
| "Annika skipping breakfast"            | [meal_proposer]                                           |
| "Jukka sleep degraded"                 | [meal_proposer, wellness_proposer, chat_brain]            |
| "Anniversary in 3 weeks, nothing planned" | [romantic_proposer]                                    |
| "Katy mentioned wanting to try X restaurant" | [romantic_proposer, meal_proposer]                  |
| "Dog boarding before June 12 trip"     | [personal_admin, dayflow_orchestrator]                    |
| "Trending device → gift for Jouko"     | [chat_brain]                                              |
| "Memorial Day pickup shift"            | [dayflow_orchestrator]                                    |

## Friction detection — concrete patterns to watch for

Friction is what the family wears on but often can't see themselves:

- **Recurring negative signal about the same subject.** A single mention
  of "Annika skipped breakfast again" is noise. The third mention in two
  weeks is friction — raise the concern even though no individual mention
  was alarming.

- **Compounding cross-signal decline.** Grades trending down + breakfast
  skipping + a parent mentioned someone "seemed off" → high-severity
  concern even though each signal alone is low. Cross-domain accumulation
  beats single-domain spikes.

- **Friction in casual language, not formal complaints.** Watch for
  repeated passing remarks: "I'm so tired again", "the dogs were a pain",
  "we never have time for X", "work has been brutal." Friction often
  hides in `recent_passing_mentions`.

- **Workarounds the family has normalized.** Eating in the car, skipping
  breakfast routinely, outsourcing things they used to enjoy. These LOOK
  stable but are friction-as-default. Surface them even if no one is
  complaining — that no one is complaining is the point.

## Day-dominating things — what reshapes a day's rhythm

Identify these in the next 3-7 days. Other concerns should be sensitive
to them (a meal proposal during a family visit looks different from a
normal Wednesday).

**On-calendar:**
- Family visits, performances, recitals
- Travel, medical appointments, big work deadlines

**Off-calendar (extract from chat / dayflow / KG):**
- Anticipated events someone mentioned but didn't put on calendar
- Mood-loading context ("work has been brutal this week")
- Backchannels: family/friend situations bleeding attention into the
  household (a parent's illness, a friend's loss)

Each dominating thing produces either an `anticipated_need` concern
(prep required) or a `schedule_collision` concern (cascading shifts).
Reference it in the `notes` of any other concern it touches — proposers
need to know.

## Inference permission — example

You may make inferences from indirect signals when the inference is
specific and the signals are real. Example:

Signals individually:
- Katy mentioned "I'm tired" twice this week (chat).
- Sleep log shows bedtime past midnight 4 of 7 nights.
- Peter said "mom seemed off" (chat, two days ago).

Inference: Katy mood/sleep concern.
- severity: medium
- addressable_by: [chat_brain, meal_proposer, family_proposer]

The `evidence` array must point to the actual signals. The inference
lives in `notes`. Never elevate a hunch to a fact — but a well-evidenced
inference is exactly what you exist to make.
