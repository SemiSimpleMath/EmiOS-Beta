# Agents look things up instead of being handed everything (2026-10-19)

Status: **proposal, no code.** Follows the 2026-10-19 review of `MultiAgentManager`, `Planner`,
`WorkPlanner` and the emi_team / work_emi_team planners. The question was what the assistant can take from coding
agents, which do much of their work by reading a few files they keep up to date.

## The idea in one paragraph

A coding agent's prompt carries an **index**: one line for each skill and for each note it keeps. It
reads the full text only when the work calls for it. It also **writes notes** about how things get done
("X lives in Y", "tool Z can't do that"), which the owner curates. The assistant already has the reading tools
(`discover_skills`/`read_skill`, `pod_search`/`pod_fetch`, `ask_kg`, `work_graph_*`). Two things are
missing:

1. Skills are pasted whole whether or not they apply.
2. There is nowhere to keep know-how that a run learned.

This document proposes both changes, plus three smaller fixes the same review found.

## What exists today

| Kind of thing | Where it lives | How an agent reaches it |
|---|---|---|
| Recipes for using tools (skills) | `skills/<name>/SKILL.md`, `skills/private/` | pasted into the system prompt, or `discover_skills` → `read_skill` |
| Owner-written instructions | `resources/instructions/*.md` | pasted as `resource_*` context items |
| Facts about the household | beliefs (`belief_intake_beliefs`), the KG, entity cards | entity cards pasted; `ask_kg` |
| Stored artifacts | pods | `pod_search` → `pod_fetch` |
| What this work object did | the work graph | pasted projection; `work_graph_peek` → `work_artifact_fetch` |
| What a concern has been through | the concern's notes | pasted when the work cites the concern |
| **How things get done, learned in a run** | **nowhere** | — |

## Part 1. Skills: show the index, read the body when it applies

### How skills reach a prompt now

`ContextInjector._resolve_skills_with_provenance` (`app/assistant/agent_runtime/services/context_injector.py`)
fills the `skills` dict from four paths:

1. **Static:** `config.yaml` `skills:`. These are added on every call, with no condition.
2. **Keyword auto-inject:** only if `accept_auto_skills: true`. It matches the skill's
   `auto_inject_when.task_keywords` against `task` / `incoming_message`.
3. **Caller-supplied:** `skills_input` on the blackboard.
4. **Scope-stamped:** `scope_context.skills.always_inject`.

The emi_team and work_emi_team planners render `{% for skill_name, skill_body in skills.items() %}`,
so whatever lands in the dict is pasted.

### What that does in practice

The owner understood that pod skills are added only when the agent carries a pod. That is how path 2
works, and `bash_team::planner` uses it. But three planners declare `pod-finder` statically and set
`accept_auto_skills: false`: `emi_team::planner`, `work_emi_team::planner` and `devices::planner`. The
skill is 147 lines.

The evidence comes from the Friday Night Meats send on 10-16 (`logs/emi_logs_5732_agents.log`, 19:19 to
19:23). All four `work_emi_team::planner` system prompts carried the whole pod-finder body. The task was
to send an approved email, and the user prompt held no pod.

The pasted body also points that planner at tools it does not have:
- It says to "call bash_manager instead" for files on disk.
- It says to "fall through to kg_query" for multi-hop questions.

Neither tool is in that planner's tool list. A skill written for one team, pasted into another, gives
instructions the agent cannot follow.

There is a second problem. The `discover_skills` contract tells the agent that relevant skills
auto-inject when keywords match, so it should not look unless no skill block is visible. For these
three planners auto-inject is off, so that statement is false.

Static skills by agent, from a scan of every `config.yaml`, with line counts:

| Agent | Static skills |
|---|---|
| emi_team, work_emi_team, devices planners | critic-handling 13, pod-finder 147, emi-acting-as-herself 26, emi-values 31 |
| kg_investigation, kg_resolution planners | kg-conflict-triage 149 |
| bash_team planner | filesystem-layout 70, critic-handling 13 (plus auto-inject) |
| room chat_gate, response_formatter | slack-formatting 46 |
| work_architect | notification-release 21 |
| others | critic-handling 13 only |

### Proposal

Split skills by what they are:

- **Who the assistant is and how she behaves** (`emi-acting-as-herself`, `emi-values`, `critic-handling`): these
  apply on every turn. Keep them static and pasted in full.
- **How to do a kind of task** (`pod-finder`, `kg-conflict-triage`, `filesystem-layout`,
  `slack-formatting`, …): replace the pasted body with **one index line** (the skill's name and its
  `description`). The agent calls `read_skill` when the line fits the task. The body then enters
  history as a tool result, so the summary node's existing rules (pin, hide) apply to it.

Concretely:

- `config.yaml` keeps `skills:` for always-on skills. A new `skill_index:` key lists the skills whose
  line goes in the index. The index renders from a shared template in the cache-stable system prompt.
- The `discover_skills` contract stops promising auto-injection. It says what the prompt actually
  holds: the index, and `read_skill` to open an entry.
- Every skill in an agent's index must name only tools that agent has. Check this when the config
  loads, against the agent's resolved tool list. It is a construction check, like `state_map`
  validation, and not a runtime gate.

#### Why not switch these planners to keyword auto-inject (path 2)?

1. **It is word matching on prose.** That is what the no-regex rule (2026-10-09) bans.
2. **It decides for the agent from outside.** The index gives the agent the context and lets it decide,
   which fits "context, not gatekeepers".

Retiring path 2 everywhere is a separate decision; see the open questions.

#### Tension with "original context over summaries"

That rule is about a concern's or the owner's own words reaching the agent, not paraphrased. An index
line plus the body read on request still delivers the original text, at the moment it is relevant.

The real risk is a mini-tier planner that does not open a skill it needed. Measure it (below) before
converting all three planners.

## Part 2. A home for know-how a run learned

### What is missing

Each place that holds facts holds one particular kind, and none of them is a home for know-how:

- **Beliefs** are declarative facts about the household, and must stay that way ("beliefs are
  declarative, never instructions").
- **A concern's notes** cover that one concern.
- **A finalizer's judgment** stays on one node of one work object.
- **Resources** are written by the owner.

So nothing holds "the Friday Night Meats link and participant list are in the Google Calendar event",
or "there is no Gmail-draft tool; hold the prepared text in an artifact instead". Each run works these
out again.

### How much this shows up in beliefs today

It is less than it looked in the review. Of 666 beliefs, 43 matched a where-to-find phrase search
(findable, found in, located, stored in, account, link, …). Read one by one, **only one is a how-to**:

- **B351:** "…The meeting link and participants are findable on google calendar."

The rest are preferences or facts, as they should be (B265: the owner expects contact details to be
looked up; B625: use the default sender account). So beliefs are mostly used correctly. B351 is what
happens when the only place to write something down is the wrong one.

The 10-16 prepare step learned a second piece of know-how that is now buried in one node's judgment:
"A Gmail draft was not created because no draft-creation tool was available."

### Proposal

- **Store:** one Markdown file per note under `resources/know_how/<slug>.md`. Frontmatter holds
  `name`, a one-line `description`, `written` (local date) and `source` (the work id and node the
  note came from, as a ref). The body is a few sentences in plain prose.
  - Files rather than a table: the owner reads and edits them like the `.md` files his coding agents
    keep, and git shows every change.
- **Index:** the planners' system prompt lists `name: description`, one line per note, from a shared
  template next to the skill index.
- **Read:** an agent opens a note's body by name. Reuse `read_skill` with notes registered as a second
  kind, or add a small `read_know_how`; see the open questions.
- **Write:**
  - When a work object ends, one agent reads the finalizer's prose accounts for its nodes, plus the
    existing index. It proposes at most a few notes:
    - something found that a later run would need (where a thing is, which manager has the tool);
    - something learned that will not change soon (a tool does not exist, a source needs a login).
  - It may also propose editing or retiring an existing note.
  - It writes no notes about the household; those are beliefs, and the brain owns them.
- **Approval:** each proposed note reaches the owner as **one ticket** with the note's exact text, and
  is written only on approval. Ticket answers are already part of the owner's day. That is unlike a
  growing file to review: `resource_learned_skills_proposed.md` is 113 KB, and its last two weekly runs
  (09-28 and 10-05 windows) proposed nothing.
- **Agent:** it gets its own directory, `app/assistant/agents/know_how/proposer/`, with Jinja prompts.
  Code does the filing; the agent decides only the text.

### Measure before building

The note index costs a few lines of every planner prompt, plus an agent call at the end of each work
object. Measured averages are 5.5k to 8.8k planner input tokens, with a maximum of 34k
(`work_web_manager::planner`). The prompt has room, so cost is not the question. The question is
whether it pays off.

**Phase 0 (read-only):**
- Read the finalizer accounts of the last two weeks of work objects.
- Count runs that rediscovered something an earlier run had already found or failed on: the same
  lookup, the same missing tool, the same dead end.
- If that happens rarely, stop here and keep only Part 1.

## Part 3. Smaller fixes from the same review

These are independent of Parts 1 and 2. Each is small.

1. **Delegation briefs are paraphrased by an extra model.**
   - A manager's `action_input` is prose, so it fails `Planner._planner_arguments_satisfy_tool_form`.
     `shared::tool_arguments` then rewrites it ("light normalization") before the sub-manager sees it.
   - On 10-16 the approved email, recipients and all, went through that model on its way to
     `work_personal_admin_manager`. That is the case the no-paraphrase-after-planner rule exists for.
   - **Proposal:** for manager tools (inputs `task` + `information`), code puts the prose into `task`
     verbatim and sets `information` to `''`. Leaf tools keep the current path.
2. **The action budget the planner is told is wrong.**
   - Several planners' `user.j2` hardcode `Action Count: {{ action_count }}/20`. Nothing enforces 20,
     and both emi_team managers allow `max_cycles: 80`.
   - **Proposal:** render the limit from the manager's config, or drop the "/20".
3. **The work-graph read tools show "No description available."**
   - `ToolRegistry.get_tool_description` reads only `prompts["<name>_description"]`, while
     `register_work_tools` writes the text to `tool_contract["description"]`. The descriptions exist
     but never reach the planner.
   - Recorded in `docs/design/bug_list_2026-09-18.md`, OPEN. These four tools are exactly the
     look-it-up tools this document relies on.

## Order of work

1. **Part 3.3**: tool descriptions → verify: the next rendered planner prompt shows all four
   descriptions.
2. **Part 3.1**: verbatim manager briefs → verify: an `_agents.log` delegation shows no
   `shared::tool_arguments` call, and the brief arrives word for word.
3. **Part 3.2**: action budget line.
4. **Part 1** on `work_emi_team::planner` first → verify over a few days:
   - count how often it calls `read_skill` for pod-finder;
   - compare with tasks that actually involved pods;
   - then convert the other two planners.
5. **Part 2, phase 0** measurement → owner decides whether to build the rest of Part 2.

## Open questions for the owner

1. **Part 1:** convert only the three planners, or also retire keyword auto-inject (path 2) everywhere,
   since it is prose word-matching?
2. **Part 2 read path:**
   - (a) `read_skill` with notes as a second kind in the skill registry — no new tool, but notes and
     recipes then share a catalog;
   - (b) a separate `read_know_how` — one more tool, but the two kinds stay apart.
   - Recommendation: (b).
3. **Part 2 approval:** a ticket for each note, or a batch per day?
4. **Part 2 scope:** do notes go to every planner, or only to the work planners that dayflow runs?
5. **B351:** once notes exist, does its "findable on google calendar" half move to a note, leaving the
   belief as "each participant should be emailed the link"? That is a belief change, so it is yours to
   decide.
