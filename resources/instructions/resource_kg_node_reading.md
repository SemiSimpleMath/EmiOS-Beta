## How to read a KG node — present-tense canonical + bitemporal validity

Every node's `label` and `original_sentence` is canonical-present-tense.
"Jukka lives in Scottsdale." "Mary is pregnant." "Jukka works at Seyfarth."
The present tense is a property of the FORM, not a claim about NOW.

Every node carries a validity window via `start_date` and `end_date`
(plus optional `start_date_confidence` / `end_date_confidence` /
`start_date_prose` / `end_date_prose`). Read the dates BEFORE you decide
what the node is asserting.

### How to interpret the dates

- `end_date` is in the past (before today) — the claim was true during
  that era and is NOT a current claim. "lives in Scottsdale" with
  `end_date: 1992-06-01` is a historical residence.
- `end_date` is null and `start_date` is set — the era is open, treat
  as currently true.
- Both dates are null — era unknown; treat as a soft claim. Do NOT
  assume currentness.
- `start_date` in the future — a planned/expected era; not yet active.

### Same-label nodes with different dates are different things

Two `lives_in` States for the same person, different cities,
non-overlapping date windows = sequential residences, both correct.
Two `Marriage` Events with different start/end = different marriages
or a re-marriage. Two `Pregnancy` States with different dates =
distinct pregnancies. Two `works_at` States, different employers,
non-overlapping = career history.

This is a reading rule, not an action rule. Whether to merge,
contradict, rewrite, or write prose based on this reading depends on
your role and is covered by your own prompt.

### Default

When in doubt: check `start_date` and `end_date`. They are the truth.
The sentence's tense is just a storage convention.
