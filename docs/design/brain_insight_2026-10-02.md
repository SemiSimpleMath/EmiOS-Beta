# Seeing the brain work: a story for the owner, a rewindable flow for the developer (design, 2026-10-02)

Status: agreed 2026-10-02. Built 2026-10-02 (all three steps; tests pass; awaiting a restart to verify
live). Build order: 1 run records, 2 the developer's flow page, 3 the stories and the owner's page.

## What the owner asked for

"We need a serious insight into how the brain is working. I would like the user facing side of it, that
is just cool to look at and the dev facing where i can see every little detail."

The owner's view, in their words: "you see the high overview summary of all the things coming to Emis
brain then you see summary of the reasoning with each item like what created a new concern and then for
each concern that has been sent somewhere you see summary of what happened/is happening to that concern
item and then for the actual brain that reasons about the concerns later you see like the summary about
what tasks are proposed and what will be the concerns faith going forward. you will need auxilary agents
to write all the summaries."

The developer's view: "flow diagram with ability to click into the agents and see how data flows
through ... you could rewind and see what went through each stage at what time."

## What is recorded today, and what is missing

Recorded: every model call of the brain's agents, with its prompts and answer (`brain_calls`, tagged by a
context variable: stage, concern, events); the gate's route of each event (`brain_events`); each
matter's decisions (`brain_matters`); the concern's brief, plan and journal on the concern.

Missing:
- **Runs.** Nothing groups what one wake of the brain did; calls are found by time and tags.
- **Code's decisions.** A refused answer and its correction, a candidate suppressed by code, a belief
  whose owner was found by code, why a brief or plan was stale, a handoff held back or an occurrence
  skipped as past: these are log lines or nothing.
- **The planner's tool results.** Its chosen tool and arguments are in its recorded calls; what the tool
  returned is seen only inside the next prompt.
- The day pass's calls carry no stage, and the selector's and writer's are kept only 50 deep.

Reading these back out of the text logs would mean pattern-matching free prose that changes whenever a
message is reworded. They are recorded instead.

## 1. Run records

- `brain_runs`: one row per wake: id, started_at, ended_at, trigger (`boot`, `event`, `timer`), the
  run's summary (what `run_brain` returns), error.
- `brain_steps`: one row per decision code made: run_id, at, stage, what, subject (a concern id, an
  item id, event ids), detail (JSON). Recorded where the decision is made:

| stage | steps |
|---|---|
| gate | an answer refused (its problems); the page's routes; a page that failed |
| brain | an answer refused; the matter's decisions; a matter that failed |
| door (inside brain or day pass) | a candidate suppressed by code (the owner settled it); created with nothing to compare; an answer refused; each candidate's outcome |
| brief | why each concern's brief is stale (none yet, its record changed, its hold passed); an answer refused; written; failed |
| plan | why each plan is stale; each tool result; an answer refused; written; failed |
| handoff | handed (item id, what was handed); held back and why; an occurrence not handed because it passed |
| day_pass | placements refused; placed; owners found by code; candidates sent to the door; written; failed |

- The run id rides in the same context variable as the other tags, so every model call made during a
  wake carries it. A call made outside a wake (the noticer) has no run; a step outside a wake is not
  recorded.
- The day pass runs under `stage=day_pass`; the selector and writer join the brain's agents, whose calls
  are kept whole.
- Recording is observability: a failed write is logged as an error and never fails the brain.

Verify: one live wake shows every stage it ran, each with its reasons.

## 2. The developer's page: `/brain/flow`

- **Diagram** of the brain's stages: inbox and gate → brain (door) → briefs → planner (planner, tools,
  decider) → handoffs → the steward in dayflow; the day pass (selector → writer → owners, door) feeding
  the concerns; the stories told from the concerns. Drawn from a fixed list in the route; the brain's
  stages are code, not a manager's state_map, so there is no config to read it from.
- **Time scrubber** over the runs: earlier/later, a slider over runs that did something, idle runs on
  request. The selected run lights the stages it ran, each with a count of what passed through.
- **A stage opened** lists, in time order, its model calls (system prompt, user prompt, answer) and code
  steps (what was decided and why). The steward's answer to a handoff is found by the item id.
- **Filter by concern**: the runs that touched one concern, so a story on the owner's page links to the
  record behind it.

Verify: rewinding to the 17:35 day pass of 2026-10-02 shows the door's refused answer and its
correction.

## 3. The stories and the owner's page: `/mind`

Three summarising agents (`subconscious::story_matter`, `story_day`, `story_concern`), written for the
owner, plain and short. Each story is stored with the fingerprint of what it was written from
(`brain_stories`: kind, key, basis, written_at, body, error) and rewritten only when that changes; a
failed one waits for a change, as briefs do. Run by the wake as its last stage; opening the page costs
nothing.

| layer | agent | written from | holds |
|---|---|---|---|
| What came in | story_day, per local day | the day's matter stories, counts by source, concerns the day pass made | a headline and an overview |
| How each item was read | story_matter, per matter (once) | the events, the gate's routes, the brain's decisions, where new concerns landed | a headline and one line per event |
| What happened to each concern sent somewhere | story_concern, per open concern with a brief | the brief, the work attached and its judgments with the owner's replies, the handoffs | the story so far |
| Where it is going | story_concern | the plan (matter) or the readiness rule (standing) | the tasks proposed now and the concern's likely fate, with a short state label |

The page: a ribbon of the day's flow (came in → read → concerns touched → sent to dayflow → done); the
day's overview; each matter with its lines, tagged by what happened (new concern, joined a concern,
noted, not worth tracking: tags from the data, words from the agent); each concern as a card (what
happened, proposed now, going forward), the day's routines apart from the matters; every card links to
`/brain/flow` filtered to it.

Verify: today's stories read correctly against the records they come from.
