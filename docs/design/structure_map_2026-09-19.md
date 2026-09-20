# Shared manager runtime reading map — 2026-09-19

## Scope and method

Follow-up to the Dayflow documentation/recovery work. Code is the source of
truth. This pass updates runtime comments, coding-agent guidance, and developer
documentation; stronger intended guarantees are retained as deferred findings.
No executable Python, prompt, runtime configuration, schema or data changes were
made in this pass. Earlier Dayflow changes in the working tree are separate.

Full means the file was read end to end (in chunks for larger files), not that
every transitive dependency or live configuration was certified. Line counts are
snapshot sizes after this pass, not stable citations. Searches located evidence;
absence claims are limited to the complete implementations read here.

## Reading log — full files

| File (relative to repository root) | Lines | Coverage |
|---|---:|---|
| `AGENTS.md` | 218 | Full |
| `EXTENDING.md` | 90 | Full |
| `docs/design/doc_improvement_task_2026-09-18.md` | 170 | Full |
| `docs/design/bug_list_2026-09-18.md` | 550 | Full |
| `docs/architecture/02_MANAGERS.md` | 279 | Full |
| `docs/architecture/02b_RUNTIME_DATA_CONTRACT.md` | 162 | Full |
| `docs/architecture/04_CONTROL_NODES.md` | 262 | Full |
| `docs/recipes/ADD_A_MANAGER.md` | 317 | Full |
| `skills/extending-emi-managers/SKILL.md` | 255 | Full |
| `app/assistant/manager_classes/MultiAgentManager.py` | 831 | Full |
| `app/assistant/manager_runtime/manager_invoker.py` | 225 | Full |
| `app/assistant/manager_runtime/request_preprocessor.py` | 267 | Full |
| `app/assistant/manager_runtime/mam_instance_manager.py` | 397 | Full |
| `app/assistant/manager_runtime/mailbox.py` | 319 | Full |
| `app/assistant/manager_runtime/services/scope_adapter.py` | 728 | Full |
| `app/assistant/manager_runtime/services/tool_scope_service.py` | 550 | Full |
| `app/assistant/manager_registry/manager_registry.py` | 27 | Full |
| `app/assistant/multi_agent_manager_factory/MultiAgentManagerFactory.py` | 145 | Full |
| `app/assistant/agent_registry/agent_loader.py` | 156 | Full |
| `app/assistant/validation/agent_validator.py` | 377 | Full |
| `app/assistant/agent_classes/Delegator.py` | 57 | Full |
| `app/assistant/lib/blackboard/Blackboard.py` | 262 | Full |
| `app/assistant/agent_runtime/services/agent_input_applier.py` | 99 | Full |
| `app/assistant/agent_runtime/services/agent_result_applier.py` | 135 | Full |
| `app/assistant/agent_runtime/services/flow_controller.py` | 122 | Full |
| `app/assistant/agent_runtime/services/final_answer_normalizer.py` | 235 | Full |
| `app/assistant/control_nodes/control_node.py` | 138 | Full |
| `app/assistant/control_nodes/tool_caller.py` | 498 | Full |
| `app/assistant/control_nodes/tool_result_handler.py` | 791 | Full |
| `app/assistant/control_nodes/tool_return_router.py` | 47 | Full |
| `app/assistant/control_nodes/final_answer_node.py` | 65 | Full |
| `app/assistant/control_nodes/manager_exit_node.py` | 77 | Full |
| `app/assistant/control_nodes/graceful_exit_control_node.py` | 133 | Full |
| `app/assistant/multi_agents/kg_mutation_manager/config.yaml` | 132 | Full |
| `app/assistant/agents/kg_mutation/planner/config.yaml` | 26 | Full |
| `app/assistant/agents/kg_mutation/final_answer/agent_form.py` | 59 | Full |
| `app/assistant/lib/tools/work_emi_team_manager/work_emi_team_manager.py` | 30 | Full |
| `app/assistant/lib/tools/work_emi_team_manager/tool_contract.json` | 65 | Full |
| `app/assistant/multi_agents/dayflow_dispatch_manager/config.yaml` | 90 | Full |

## Partial and unverified coverage

- `CLAUDE.md`: header read; mirrored body verified identical to the fully read
  `AGENTS.md`, including this pass's edits.
- `docs/INDEX.md`: core architecture/recipe navigation read; unchanged.
- Earlier `structure_map_2026-09-18.md`: consulted previously, not recertified.
- Agent base class, Planner internals, room ingress/session implementation,
  approval/access helpers, ManagerInterface, resource loading, scope-state
  helpers, registry discovery internals and all other control-node families:
  not fully reviewed in this pass. References to them are not whole-file
  certification. The shared boundaries described here were read at their callers.
- Domain-specific catalog entries and room-flow illustration in `04_CONTROL_NODES`
  retain earlier coverage; this pass verifies the shared dispatch/result segment.
- Existing historical bug entries retain their recorded status; this pass does
  not re-run their verification or claim they remain unchanged in all callers.

## Call chains and state ownership

1. ManagerRegistry reads directory `config.yaml`; factory filters the tool
   registry, copies the agent registry, constructs a fresh manager/Blackboard.
   AgentLoader registers declared agent/node instances; role aliases do not load
   additional instances. Standard-agent classes come from registry definitions.
2. ManagerInvoker registers the invocation, writes its id/start time to the
   blackboard and publishes the started event. Preprocessor normalizes task
   input and may return a skipped result before scope adaptation or the loop.
   ScopeAdapter resolves scope, applies receiving-manager policy, projects task
   tool restrictions; preprocessing/scope exceptions propagate after unregister.
3. request_handler seeds task/information then all Message.data keys, overlays
   explicit room/scope/config state, initializes tool visibility and pipeline
   state, pushes a root context and hydrates seeded history. It does not reset an
   existing manager. Exceptions enter error-exit handling; finally unwinds the
   request context. Returned data must be read from ToolResult.
4. Each loop boundary drains agent-injection mail, checks cancel/budgets/exit/error,
   calls deterministic Delegator, resolves one role alias, and activates the chosen
   instance. Standard AgentInputApplier clears next_agent; control nodes own their
   route writes. AgentResultApplier filters reserved output keys unless declared.
5. FlowController stores action results/signals or pending calls. ToolCaller
   requires matching action/tool_arguments, checks tool scope/task restrictions,
   and invokes the tool synchronously. Result handling is a direct call; standard
   tools stay in their scope. Agent calls push a child; the result handler reads
   the result before popping and logs it in the parent scope. ToolReturnRouter
   writes resume_target and leaves the next hop to state_map.
6. Final nodes select/normalize the answer. handle_exit builds ToolResult and may
   attach selected raw data. Cancel returns manager_aborted directly; error/budget
   exits attempt another routing loop, with a final_answer fallback if that fails.
7. Finally, invoker unregisters and clears the queue then present. Mailbox TTL is
   enforced on drain, not by a timer. Cancellation is a direct global write and
   is observed at a boundary after the synchronous call returns.

## Claim review

Statuses describe the old claim; the affected guidance/comments were corrected.

| Claim | Assessment | Source evidence / disposition |
|---|---|---|
| Blackboard is allocated per request automatically | Overstated | Constructor allocates it; fresh-instance caller contract documented |
| All agent output keys are written, with no reserved-key filter | Stale | AgentResultApplier and AgentInputApplier guards documented |
| Every activation automatically consumes next_agent | False | Standard input path clears; nodes are responsible; Delegator honors existing route |
| All missing route targets and return-control edges fail construction | Overstated | Name membership only; R1 |
| No scope always raises out of manager invocation | Overstated | Adapter resolution/derivation and request_handler catch distinguished |
| SKIPPED only applies to task-file tasks | False | Preprocessor checks every resolved task; early-return ordering documented |
| max_cycles measures all LLM calls | False | Only loop-selected non-ControlNodes increment; internal calls excluded |
| ToolCaller schedules ToolResultHandler via next_agent | False | Direct handler calls; tool and agent scope paths distinguished |
| ToolReturnRouter routes straight back to caller | False | Writes resume_target; state_map chooses next hop |
| All aborts have manager_aborted type | False | Default error fallback; R2 |
| Mailbox carries cancellation / automatically ages out all queues | False | Direct global cancel; drain-only TTL; R3 |
| Scope pop is atomic across threads | False | Two owning-thread list pops; concurrency comment corrected |
| Visibility and narrower input obey every execution restriction | Overstated | Scope/task filtering differences; R4 |
| final_answer_raw always preserves all sibling form fields | Overstated | Selected payload can exclude domain fields; R5 |
| Exit wins when set by final budgeted activation | Not guaranteed | Budget checks precede exit; R6 |
| Manager directory needs __init__.py / managers are callable as agents | False | Registry requires config; static tool wrapper or direct invocation |
| ScopeAdapter child tools still have the old undeclared-surface gap | Stale | Config tools fallback and subtree grants implemented; old migration prose corrected |

## Open questions and next passes

The six established implementation gaps are tracked as R1–R6 in
[the existing bug list](bug_list_2026-09-18.md#shared-runtime-follow-up-2026-09-19).
They are not fixed by documenting current behavior.

Further questions, not established end-to-end defects in this pass:

- RequestPreprocessor resolves task resources before ScopeAdapter and constructs a
  resource filter with an empty denied list. Review ResourceManager enforcement
  and task callers before claiming whether a caller denial can be bypassed.
- AgentLoader's own `agents` dict is initialized but not populated by its loader;
  instances are registered on `agent_registry`. Inspect callers before retiring
  or repairing its local lookup helpers. The developer recipe now uses the registry.
- The old ScopeAdapter migration prose proposed clamping child authority with
  min(parent, child); current code rejects an expansion. Treat adopting clamping
  as an unresolved design proposal, not behavior already implemented.
- Review direct activation paths in Agent/Planner and control-node families for
  budget/cancellation expectations beyond the manager boundary.
- The dispatch-manager config still describes same-status dispatch claims as an
  exclusion fence; the historical LIVE 3 finding records the contrary store
  behavior. Resolve the claim semantics during the work-object lifecycle pass
  before certifying that config's comments.

Next logical pass: work-object lifecycle and persistence, then tools/scope and
room/transport contracts. Keep full/partial/unverified coverage explicit.

## Verification

- Compared pre-pass and post-pass Python ASTs after removing docstrings: all 12
  edited source files retain identical executable structure.
- AGENTS/CLAUDE mirrored bodies are identical; manager skill quick validation passed.
- Isolated execution of actual source methods (without app bootstrap) confirmed
  R1's admitted invalid alias, R2's fallback result shape, and R3's post-after-clear
  queue/TTL behavior. Other findings are source-traced, not live reproductions.
- Documentation examples/references and final diff checked against the named
  implementations. No integration suite needed for comment/documentation-only
  changes; earlier Dayflow behavior changes are outside this verification claim.
