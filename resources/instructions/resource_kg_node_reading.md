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
  `end_date: 1992-06-01` is a historical residence, not a contradiction
  to a current Irvine residence.
- `end_date` is null and `start_date` is set — the era is open, treat
  as currently true.
- Both dates are null — era unknown; treat as a soft claim. Do NOT
  assume currentness; ask or escalate before contradicting it.
- `start_date` in the future — a planned/expected era; not yet active.

### Same-label nodes are NOT automatically duplicates

Two seemingly-conflicting nodes are NOT duplicates and NOT
contradictions if their date ranges differ:

- Two `lives_in` State nodes for the same person, different cities,
  non-overlapping date windows → both correct, sequential residences.
- Two `Marriage` Event nodes with different start/end → different
  marriages OR a re-marriage; not duplicates.
- Two `works_at` States, different employers, non-overlapping → career
  history, not a contradiction.
- Two `Pregnancy` States with different dates → distinct pregnancies.

### When IS it a contradiction or duplicate?

Only flag a contradiction when the date windows OVERLAP and the claims
are mutually exclusive at the same point in time. You can't physically
live in two places simultaneously, you can't be employed full-time at
two employers at the same instant — though even there, watch for
remote-work / consulting / dual-residence cases.

Only propose a merge of two nodes when their date windows match (or
both are null AND there is no signal that two distinct events are
being conflated) AND the labels/predicates describe the same fact.
Same predicate + non-overlapping dates = sequential, keep both.

### When you write prose ABOUT a node

- Past era (`end_date` in past) → past tense in your prose. "Jukka
  lived in Scottsdale" not "Jukka lives in Scottsdale".
- Open era → present tense.
- Always ground a claim with the date range when both ends are known
  ("from 1990 to 1992", "since 2010", etc.). Never strip the dates
  from a present-tense node and quote it as a current fact.

### Default rule

When in doubt: check `start_date` and `end_date`. They are the truth.
The sentence's tense is just a storage convention.
