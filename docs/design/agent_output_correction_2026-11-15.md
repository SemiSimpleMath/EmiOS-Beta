# Agent output correction (2026-11-15) — BUILT 2026-11-15 (uncommitted, needs restart)

Owner, 2026-11-15: "build a correction module that all agents use if it is enabled for them. This should be
standardized. ... agent has or does not have an extra file in their folder probably next to agent_form.py that
contains a validation function. These are all auto loaded during boot if they are there." And: "this should not
balloon to a big thing."

## Why

On 2026-11-15 the routine writer placed B558 at 11:00 PM `until: "midnight"`. `day_pass._problems` refused it, and the
one correction got the same answer again, so the brain run failed (bug list, "Routine writer writes `until: midnight`").
The retry was a fresh call: the same system and user prompt with a `correction` string rendered into the user prompt.
The model never saw the answer it was told to correct. About 20 call sites hand-roll the same retry (listed in that
bug entry). Each owns its own loop, wording and failure.

`llm_client._revalidate_or_reask` is a second correction mechanism. On a Pydantic validation failure it re-asks once,
appending the error as a user turn, again without the model's answer. This design replaces it (one implementation per
mechanism).

## What exists today (read 2026-11-15)

- `AgentRegistry._load_all_agent_configs` walks `app/assistant/agents/**/config.yaml`. For each folder it loads
  `prompts/` (system.j2, user.j2), `agent_form.py` (the Pydantic `AgentForm`, stored as `structured_output`) and
  `input_schema.py`, all into `self.configs[name]`.
- `Agent.action_handler`: `_build_messages` → `[system, user]`; `_execute_model` → `llm_client.call_structured_output`
  (transient retry, then the Pydantic re-ask); `_finalize_execution` → `process_llm_result` (blackboard, audit
  message, flow control). Returns `ToolResult(data=result_dict)`.
- Callers such as `day_pass._judge` read `.data`, run their own `_problems`, and on failure call the agent again with
  `correction` in `agent_input`. Each such agent's `user.j2` carries a CORRECTION block.

## The design

### 1. One optional file per agent: `validate.py`, next to `agent_form.py`

```python
# app/assistant/agents/dayflow_routine_writer/validate.py
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"judgments": "belief_key"}   # optional: how list items are identified, for partial answers

def validate(answer: dict, agent_input: dict) -> list[Problem]:
    ...
```

- `validate(answer, agent_input)` returns the problems, empty when the answer is good. It reads what it needs to
  check against (the batch's belief keys, the line labels) from `agent_input`; the caller puts those there as plain
  data alongside what the prompt renders. The file holds what each call site's `_problems` holds today, moved.
- `Problem`: `path` (where, e.g. `judgments[B558].times[0].until`), `value` (what the model wrote), `why` (in
  words, with the correct form when there is one: "not a clock time; midnight is written '12:00 AM'"), and `kind`:
  `field` (this value is wrong) or `structure` (an item missing, duplicated or not one of those given).
- `ITEM_KEYS` (optional): for each top-level list field, the field that identifies an item. Without it, every problem
  in that list is answered with the full form.

The file present = correction enabled for that agent. No config.yaml key is needed. The registry loads it at boot
like `agent_form.py` (same unique-module-name import) into `configs[name]["validator"]`. A file that defines no
`validate` fails boot loudly.

### 2. One module: `app/assistant/agent_runtime/correction.py`

Called from `Agent._run_llm_with_schema`, the model call every agent class makes (the base `action_handler`, and
EmiReminderHandler, ToolArguments and ToolArgumentsPlaywright from their own). The base `_execute_model` passes
`message.agent_input`; the subclasses that call `_run_llm_with_schema` themselves pass none, so they get the form check
only. An agent with neither a Pydantic form nor a validator is called once, unchecked.

`run(agent, messages, response_format, agent_input, call)`:
1. Calls the model. A failure whose exception chain holds a pydantic `ValidationError` is the form refusing the answer
   inside the provider (OpenAI's parse path raises; the answer itself is lost): its errors become `Problem`s with the
   refused values. Any other failure raises unchanged.
2. Otherwise checks the answer: one JSON object, then `AgentForm.model_validate` (Gemini and Anthropic return raw
   dicts), then `validate(answer, agent_input)`.
3. No problems: returns the answer.
4. Problems: one correction round. The messages are the original ones, then the model's answer as the **assistant**
   turn (when there is one), then a **user** turn rendered from `shared/correction.j2`: each problem's place, the value
   written, and why.
5. The form for that round:
   - every problem a non-structural one of a keyed item (ITEM_KEYS) in one list: a form with only that list field
     (`pydantic.create_model`). The model answers only the items named, and each returned item takes the place of the
     item with the same key;
   - otherwise the full form, and the new answer replaces the old one whole.
6. Checks the result whole again; still refused, raises `AgentOutputInvalid(agent, problems)` (a ValueError).

Each refusal is logged (WARNING with the problems). The retry goes through the same `call_llm`, so the prompt log and
`brain_trace.record_call` record it like any call.

Verified 2026-11-15: the 08:12 B558 prompt and its logged "midnight" answer replayed against the live model 3 times;
each time the correction gave `11:00 PM` until `12:00 AM`.

### 3. What goes away

- `llm_client._revalidate_or_reask` (removed): Pydantic failures become `Problem`s in steps 1-2 for every agent with
  an `AgentForm` (owner, 2026-11-15: yes), and `validate.py` adds the agent's own rules.
- `TicketComposer` (removed; `ticket_builder::composer` is a plain `Agent`) and the architect's `_ask` form-refusal
  retry: the same correction as step 1.
- At each of the ~20 call sites: its `_problems` function moves into its agent's `validate.py`. Its retry loop, the
  `correction` key in `agent_input`, the `correction` entry in `user_context_items`, and the CORRECTION block in
  `user.j2` are removed. The site calls the agent once and reads `.data`.

## Files touched

- NEW `app/assistant/agent_runtime/correction.py` (Problem, AgentOutputInvalid, correct, limited form, merge).
- NEW `app/assistant/agents/shared/correction.j2`.
- `agent_registry.py` (+ `_load_validator`, into `configs[name]["validator"]`): marked "do not modify without user
  permission" (owner approved, 2026-11-15).
- `Agent.py` (`_run_llm_with_schema` runs the module; `_execute_model` passes agent_input): marked "do not modify
  without user permission" (owner approved, 2026-11-15).
- `llm_client.py` (− `_revalidate_or_reask`).
- Per migrated agent: + `validate.py`; − CORRECTION block in `user.j2`, − `correction` in `config.yaml`.
- Per call site: − `_problems` and the retry.

## Order

1. Module, registry loading, Agent hook, shared template, tests (a validator that fails once, then passes; the messages
   of the retry are [system, user, assistant answer, correction]; a partial answer merges by key; a structural problem
   re-asks with the full form; still invalid raises).
2. Migrate `dayflow_routine_writer` (the case that failed) and `subconscious::day_placements`; replay the B558 inputs.
3. Remove `_revalidate_or_reask`, and migrate the other call sites.

## Open

- Agents whose answer drives tool actions (`action`/`action_input`): `ActionContractService` already validates those
  in `process_llm_result`; they stay out of scope.
