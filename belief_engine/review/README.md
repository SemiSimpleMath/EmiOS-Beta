# Contextual belief review: copy-only evaluation

This opt-in path evaluates the existing belief architecture against a frozen SQLite
backup. It is not registered with the nightly pipeline and does not replace it.

## Implemented flow

1. `make_working_copy` uses SQLite backup and marks the new database as an evaluation
   copy. `ReviewCopy` refuses to write the baseline or an unmarked database.
2. `belief_engine::context_search`, a standard AgentFactory agent, reads the entire
   current belief catalog. The LLM selects potentially relevant beliefs across domains;
   there is no embedding threshold, lexical match decision, or automatic merge.
3. `belief_engine::context_review` receives focal beliefs, selected context, complete
   directly attached evidence, optional dated daily/weekly insights, and coverage limits.
   It can request more catalog searches, exact belief IDs (including archived merge
   predecessors), and original master-room chat/ticket records for UTC source days.
4. The LLM proposes changes or preserves beliefs, with free-text reasoning,
   applicability and uncertainty. Operational fields constrain writes, not interpretations.
   Daily/weekly restatements are not counted as independent observations. Age informs
   the model; this path does not numerically expire or re-confirm beliefs.
5. Python validates IDs and evidence handles, owner locks and complete focal coverage.
   It fences all inspected belief/evidence versions and commits the review receipt and
   full before/after revisions in one transaction. Exact replay is idempotent. Invalid
   references go back to the model; Python never guesses a replacement reference.

No merge/delete operation exists here. Source observations, dates, counts, tags and
legacy evidence weights remain intact. Two duplicates are preferable to a wrong merge.
All behavioral prompts live in the agents' `.j2` templates; schemas use `agent_form.py`.

## Reproducing an evaluation

Create a consistent SQLite baseline with `sqlite3.Connection.backup`, then supply a
private JSON case list and optional insight records. Use absolute paths outside tracked
source directories. The baseline contains personal application data; keep all data and
results out of Git. `--run-models` explicitly enables external model calls through the
installation's configured provider. Obtain authorization for those calls and their data.

```powershell
.venv\Scripts\python.exe -m belief_engine.review.cli --baseline C:/private/baseline.db --working C:/private/reviewed.db --cases C:/private/cases.json --output C:/private/results --insights C:/private/insights.json --run-models
```

Cases: `[{"name":"example","question":"Review this belief in context.","belief_keys":["example.key"]}]`

Insights: `[{"ref":"daily:2026-01-01:0","interpretation":{...}}]`. Include source
dates/references in the interpretation. The reviewer treats these as interpretations,
not raw user statements. A full source-ingestion ledger is future work.

The CLI uses the standard test bootstrap and actual AgentFactory/model invocation,
with an isolated runtime database/data directory. Tool discovery and unrelated resource
template compilation are disabled. It does not start the app, dispatch tools, or load a
live vector store. Model inputs, outputs and validation repairs remain in private JSON
artifacts. A completed review is stored even if no belief needs changing.

## Limits and promotion requirements

- This is a pilot for reviewing existing beliefs, not a full production replacement.
  New-belief creation, splitting, durable nightly work scheduling, ingestion and Dayflow
  wiring are not implemented here. Source records remain in the frozen backup.
- Entire-catalog LLM selection demonstrates broad contextual retrieval at modest corpus
  size. It can be expensive for large stores; evaluate recall and cost before scaling.
- Source-day requests return full available days without hidden truncation. Investigations
  are bounded by review rounds and fail without writes if unfinished. A single round can
  contain multiple searches; this is not yet a total token/cost budget controller.
- Archive inspection follows exact predecessor IDs. There is no general semantic archive
  search, nor access to missing/deleted sources or other rooms. Broken legacy lineage can
  remain unresolved. Do not treat missing evidence as disproof.
- The consumer projection is saved in the review result; current Dayflow does not consume
  it. In particular, the legacy numeric confidence snapshot columns remain unchanged and
  can differ from the review's qualitative confidence. Use the review result for comparison.
- Never install the evaluated database over the live database. Production integration
  needs a migration plan, a durable source ledger/review queue, shared reader contracts,
  and human-reviewed semantic evaluations. Passing integrity tests does not prove that
  model interpretations are correct.

Tests: `app/assistant/tests/non_agent_tests/test_contextual_belief_review_copy.py`.

## Historical provenance investigation

`belief_engine.review.provenance.propose` is an opt-in, read-only proposal service.
It uses the standard `belief_engine::provenance_discover` and
`belief_engine::provenance_review` agents, with behavior in Jinja and forms in each
agent folder. It is not registered in nightly processing or ordinary chat.

Discovery receives complete focal claims/conditions and a page of source-bearing
historical/current claims. Explicit old merge references are also retrieval leads.
Neither method proves that the predecessor's sources substantiate the current claim.
Review receives the complete current claim and whole original source records with
original claim context. It assigns support, contradict, qualify, unrelated, or
insufficient per source. In particular, partial evidence does not establish all clauses
of a compound claim or a narrower condition. No source text is truncated.

The caller pages whole records and retains exact local-label to original-ID mappings.
The service requires complete, unique source coverage and exact supplied identities.
A unique supplied belief key can resolve to its local ID; ambiguous historical keys
and approximate text cannot.
One invalid result receives a bounded validation repair; a second failure raises.
Validated receipts include complete inputs, response, any repair, and a digest of
inputs, prompts and output schema. Identical completed pages reuse their receipt;
changed claims, sources, prompts or schemas require new review. Partial catalog
coverage and missing sources must be reported as unresolved, never as disproof.

This service creates private JSON review artifacts only. It does not attach evidence,
merge claims, alter weights, recalculate confidence, change dates, or write databases.
A future application path must version-fence reviewed claims and sources, preserve
original evidence identities/dates, use claim-relative valence, and account for repeated
underlying observations before affecting support. Linking an entire predecessor after
a historical split is unsafe. A receipt is an interpretation, not a new observation.

The copied-data evaluation is separate from the live store. Do not install an evaluated
database over production. Full personal inputs and receipts belong outside Git.
Focused tests: `test_belief_provenance_proposals.py`.

The historical evaluation also checks proposed full support/contradiction against the
source alone, withholding predecessor claim text from this second assessment. Otherwise
a reviewer can accidentally use the old claim itself as proof. Partial support is not
disproof: many current claims mix a factual observation with derived operational guidance.
Do not automatically downgrade or discard those claims merely because a source supports
only their factual part. Record the distinction for a subsequent whole-claim review.

For evaluation callers, sort candidate/source IDs before paging. Unordered sets can regroup
unchanged sources and defeat a page cache on restart. Retain full page receipts and an exact
per-source context index when recovering a partially completed evaluation; conflicting
judgments must remain pending. A partial per-source attribution is not a whole-claim verdict:
several partial sources may jointly support it. Whole-claim synthesis is separate work.
