import json
from app.assistant.agent_classes.Agent import Agent  # Base Agent class
from app.assistant.utils.pydantic_classes import Message, ToolResult
from app.assistant.utils.history_formatting import format_recent_history

from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.tool_registry.mcp_install_registry import list_installed_records

logger = get_logger(__name__)

# Differs from ordinary agent in 3 ways
# 1) records the message having plan_message subclass so we can tell apart messages before and after a plan
# 2) resets critique variables if any exist
# 3) Specifically references action variables to set next_agent or tool_call
class Planner(Agent):
    HISTORY_LIMIT = None

    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)

        self.tool_arguments_agent = None

    def get_tools(self) -> list:
        """
        Planner override:
        If `install_tool` is allowed, dynamically expose currently installed MCP
        tools so the planner can call them immediately after installation.
        """
        base_tools = list(super().get_tools() or [])
        if "install_tool" not in base_tools:
            return base_tools

        try:
            installed = list_installed_records(enabled_only=True)
            installed_names = {
                str(r.namespaced_tool_name).strip()
                for r in installed
                if isinstance(getattr(r, "namespaced_tool_name", None), str)
                and str(r.namespaced_tool_name).strip()
            }
            dynamic_mcp = [t for t in installed_names if t.startswith("mcp::")]
        except Exception as e:
            logger.error("[%s] Failed to resolve installed MCP tools for planner: %s", self.name, e)
            logger.debug("[%s] installed MCP tool resolution exception details", self.name, exc_info=True)
            return base_tools

        if not dynamic_mcp:
            return base_tools

        merged = list(dict.fromkeys([*base_tools, *dynamic_mcp]))

        # Respect task-level restrictions for dynamically added tools as well.
        try:
            task_allowed = self.blackboard.get_state_value("task_allowed_tools")
        except Exception:
            task_allowed = None
        try:
            task_except = self.blackboard.get_state_value("task_except_tools")
        except Exception:
            task_except = None

        if isinstance(task_allowed, list):
            allowset = {str(x).strip() for x in task_allowed if isinstance(x, str) and x.strip()}
            if "all" in allowset and len(allowset) > 1:
                raise ValueError(f"[{self.name}] task_allowed_tools cannot mix 'all' with specific tool names.")
            # If the task explicitly allows install_tool, also allow installed MCP
            # tools so planner can invoke them immediately post-install.
            if "all" not in allowset and "install_tool" in allowset:
                allowset.update([t for t in dynamic_mcp if isinstance(t, str) and t.strip()])
            if "all" not in allowset:
                merged = [t for t in merged if t in allowset]
        if isinstance(task_except, list) and task_except:
            denyset = {str(x).strip() for x in task_except if isinstance(x, str) and x.strip()}
            if denyset:
                merged = [t for t in merged if t not in denyset]

        return merged

    def build_recent_history(self, agent_messages):
        """
        Planner override:
        - format planner-visible history for prompt injection
        """
        try:
            msgs = list(agent_messages or [])
        except Exception:
            msgs = agent_messages

        # Ownership filter:
        # include only messages authored by this planner or explicitly addressed to it.
        if isinstance(msgs, list):
            filtered = []
            for m in msgs:
                sender = getattr(m, "sender", None)
                receiver = getattr(m, "receiver", None)
                sender_ok = isinstance(sender, str) and sender == self.name
                receiver_ok = isinstance(receiver, str) and receiver == self.name
                if sender_ok or receiver_ok:
                    filtered.append(m)
            msgs = filtered

        return format_recent_history(msgs)

    def _create_response_message(self, result_dict: dict):
        """
        OVERRIDE: Creates the specific messages unique to the Planner.
        This replaces the generic message from the base Agent class.
        """
        action = result_dict.get("action", "")
        is_exit_action = "exit" in str(action).lower()
        sub_data_type = ["result"] if is_exit_action else ["plan"]

        plan_message = Message(
            data_type="planner_result",
            sub_data_type=sub_data_type,
            sender=self.name,
            receiver="Blackboard",
            content=f"{self.name} created a plan: {json.dumps(result_dict)}"
        )
        self.blackboard.add_msg(plan_message)

    def process_llm_result(self, result):
        """
        The Planner's process method, which now cleanly integrates with the base class.
        """


        # ---  DEBUG Logging ---
        self._maybe_print_llm_result(result)
        # --- End of New Code --


        # Step 1: Validate input (same as parent)
        if isinstance(result, str):
            logger.error(f"[{self.name}] LLM returned a string: {result}")
            # IMPORTANT:
            # Never leak action="error" into flow control. If the LLM times out or returns
            # non-structured output, exit the flow cleanly and store the error as the result.
            result_dict = {
                "action": "return_control",
                "result": {"status": "error", "error": result},
            }
        elif not isinstance(result, dict):
            logger.error(f"[{self.name}] LLM result is not a dict: {type(result)}")
            result_dict = {
                "action": "return_control",
                "result": {"status": "error", "error": f"Invalid result type: {type(result)}"},
            }
        else:
            result_dict = result

        # Step 1b: Validate action/action_input contract before mutating state
        self._validate_action_and_action_input(result_dict)

        # --- Call the shared logic from the parent class ---
        self._apply_llm_result_to_state(result_dict)

        # Research notebook (B): mint any findings the planner emitted into pods, de-duped by
        # `unit` so refining the same thing UPDATES one pod instead of duplicating. No-op for
        # planners whose form has no findings_to_pod.
        self._mint_research_findings(result_dict)

        action = str(result_dict.get("action") or "").strip()
        _skip_actions = {"return_control", "done", "none", "step_complete", ""}
        _has_tool_action = action and action not in _skip_actions
        if _has_tool_action:
            tool_args = self._generate_tool_args()
            self.blackboard.update_state_value("tool_arguments", tool_args)
        elif action == "step_complete":
            # Step is complete — route directly to the step feeder.
            # Keep action="step_complete" on the blackboard so the feeder sees it.
            self.blackboard.update_state_value("tool_arguments", None)
            self.blackboard.update_state_value("next_agent", "spec_step_feeder")

        # --- Call its own overridden and unique logic ---
        self._create_response_message(result_dict)

        # Update last acting agent in the local scope
        self.blackboard.update_state_value('last_agent', self.name)

        if _has_tool_action:
            self.blackboard.update_state_value("calling_agent", self.name)

        self._handle_flow_control(result_dict)

        # Increment action count for this agent (local scope)
        current_actions = self.blackboard.get_state_value(f'{self.name}_action_count', 0)
        new_action_count = current_actions + 1
        self.blackboard.update_state_value(f'{self.name}_action_count', new_action_count)
        logger.info(f"[{self.name}] Action count: {new_action_count}")

        # Emit a high-signal progress fact for the UI / chat narrator.
        # The base Agent.process_llm_result does this; Planner overrides
        # the whole method, so we replicate the call here. Without it,
        # planner_decision cards never reach ProgressCurator → ChatNarrator
        # and the user gets no in-chat narration of long-running work.
        try:
            self.components.progress_emitter.emit_planner_decision(self, result_dict)
        except Exception as e:
            logger.error("[%s] Failed to emit planner progress fact: %s", self.name, e)
            logger.debug("[%s] planner progress emit exception details", self.name, exc_info=True)

        # Best-effort: publish high-signal progress to an orchestrator (if this planner is running under one).
        try:
            self._publish_orchestrator_progress(result_dict)
        except Exception:
            pass


    @staticmethod
    def _slug(text) -> str:
        import re
        s = re.sub(r"[^a-z0-9]+", "_", str(text or "").strip().lower()).strip("_")
        return s or "unit"

    def _mint_research_findings(self, result_dict: dict) -> None:
        """Mint findings_to_pod entries into durable pods (kind=research_finding), one pod per
        `unit` (re-emitting a unit upserts the same pod_id). Accumulate a `research_notebook` of
        headers (unit / one_liner / pod_id) on the blackboard so the planner sees what it has
        already captured and stops re-podding. Then clear findings_to_pod."""
        findings = result_dict.get("findings_to_pod") if isinstance(result_dict, dict) else None
        if not isinstance(findings, list) or not findings:
            return

        import hashlib
        from app.assistant.pod_store.contracts import Pod
        from app.assistant.pod_store.pod_store import PodStore

        sc = self.blackboard.get_state_value("scope_context", None)
        run = self._slug(getattr(sc, "scope_id", None) or self.blackboard.get_state_value("task", "") or "web")
        store = PodStore()

        notebook = self.blackboard.get_state_value("research_notebook", None)
        by_unit = {n["unit"]: n for n in notebook if isinstance(n, dict) and n.get("unit")} \
            if isinstance(notebook, list) else {}

        minted = 0
        for f in findings:
            if not isinstance(f, dict):
                continue
            unit = str(f.get("unit") or "").strip().lower() or self._slug(f.get("one_liner"))
            # Canonical pod URI: datapod:<snake_kind>:<[a-z0-9]{6,}>. The id is a
            # deterministic hash of (run, unit) so re-emitting a unit upserts the same
            # pod, while staying a valid pod URI matched by pod_uri.POD_URI_RE — so the
            # PodInjector and the chat linkifier recognize it. run/unit kept in metadata.
            token = hashlib.blake2b(f"{run}|{unit}".encode("utf-8"), digest_size=6).hexdigest()
            pod_id = f"datapod:research_finding:{token}"
            urls = [s for s in (f.get("source_refs") or []) if isinstance(s, str)]
            pod = Pod(
                pod_id=pod_id,
                kind="research_finding",
                tags=[t for t in (f.get("tags") or []) if isinstance(t, str)],
                one_liner=str(f.get("one_liner") or unit),
                body=str(f.get("body") or ""),
                source_refs=[],                      # PodSourceRef is {kind,id}, not a URL
                for_agents=[],
                scope_id=None,
                created_by=self.name,
                metadata={"unit": unit, "run": run, "source_urls": urls},
            )
            store.put(pod)                                  # upsert by pod_id → de-dup per unit
            by_unit[unit] = {"unit": unit, "one_liner": pod.one_liner, "pod_id": pod_id}
            minted += 1

        self.blackboard.update_state_value("research_notebook", list(by_unit.values()))
        self.blackboard.update_state_value("findings_to_pod", [])   # consumed

        # FLUSH (B): the raw tool results that fed these findings are now captured durably in pods.
        # Suppress them from the planner's working history (format_recent_history skips
        # context_suppressed) so its context shrinks as it moves to the next unit — it works from
        # the research_notebook + checklist, not the raw scrapes.
        flushed = 0
        if minted:
            scope_id = self.blackboard.get_current_scope_id()
            for m in (self.blackboard.get_messages_for_scope(scope_id) or []):
                if getattr(m, "data_type", None) == "tool_result":
                    meta = getattr(m, "metadata", None)
                    if isinstance(meta, dict) and not meta.get("context_suppressed"):
                        meta["context_suppressed"] = True
                        flushed += 1
        logger.info("[%s] research notebook: minted/updated %d pod(s), %d unit(s); flushed %d tool result(s).",
                    self.name, minted, len(by_unit), flushed)

    def _publish_orchestrator_progress(self, result_dict: dict) -> None:
        if not isinstance(result_dict, dict):
            return

        # Gate: only publish if a progress token is provided by the orchestrator.
        token = self.blackboard.get_state_value("orchestrator_progress_token", None)
        if not isinstance(token, str) or not token.strip():
            return
        orch_name = self.blackboard.get_state_value("orchestrator_name", None)
        job_id = self.blackboard.get_state_value("orchestrator_job_id", None)
        if not isinstance(job_id, str) or not job_id.strip():
            return

        progress = result_dict.get("progress_report")
        if not isinstance(progress, list) or not progress:
            return

        # With append_fields + list-extend semantics, planners should emit ONLY newly discovered items each step.
        # Treat the current `progress_report` as a delta and publish it directly.
        major_impact = any(isinstance(x, dict) and bool(x.get("major_impact", False)) for x in progress)

        mgr_name = self.blackboard.get_state_value("manager_name", None)
        mgr_name = mgr_name if isinstance(mgr_name, str) else None

        DI.event_hub.publish(
            Message(
                sender=self.name,
                receiver=str(orch_name) if isinstance(orch_name, str) and orch_name.strip() else None,
                event_topic=str(token),
                data={
                    "orchestrator": str(orch_name) if isinstance(orch_name, str) else None,
                    "job_id": str(job_id),
                    "manager_name": mgr_name,
                    "agent": self.name,
                    "major_impact": bool(major_impact),
                    "progress_items": progress,
                },
            )
        )


    def _generate_tool_args(self):
        tool = self.blackboard.get_state_value("action")
        arguments = self.blackboard.get_state_value("action_input")

        # Fast path: if the planner already produced arguments that validate
        # against the tool's Pydantic form, skip the LLM args agent entirely.
        # action_input is typed as `str` in every planner's agent_form; the
        # value the planner emits when calling a tool should be valid JSON
        # whose keys match the tool's input schema. We parse first, then
        # validate. Any failure (non-JSON, wrong shape, type mismatch) falls
        # through to the args agent unchanged.
        parsed_arguments = arguments
        if isinstance(arguments, str):
            stripped = arguments.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    candidate = json.loads(stripped)
                except (ValueError, TypeError):
                    candidate = None
                if isinstance(candidate, dict):
                    parsed_arguments = candidate

        if self._planner_arguments_satisfy_tool_form(target_name=tool, arguments=parsed_arguments):
            logger.info(
                "[%s] tool_args fast path: planner args validated against tool form, skipping args agent",
                self.name,
            )
            return {
                "target_name": str(tool).strip(),
                "arguments": parsed_arguments if isinstance(parsed_arguments, dict) else {},
            }

        # Slow path: invoke the shared args agent to normalize / fill in.
        if self.tool_arguments_agent is None:
            self.tool_arguments_agent = DI.agent_factory.create_agent("shared::tool_arguments")
        if self.tool_arguments_agent is None:
            raise RuntimeError(f"[{self.name}] Failed to instantiate required agent 'shared::tool_arguments'.")
        agent_input = {"target_name": tool, "arguments": arguments}
        agent_msg = Message(agent_input=agent_input)
        arguments_result = self.tool_arguments_agent.action_handler(agent_msg)
        # self.tool_arguments_agent.reset() this may or may not be necessary.

        if not isinstance(arguments_result, ToolResult):
            raise TypeError(
                f"[{self.name}] tool_arguments_agent returned invalid result type: {type(arguments_result)}"
            )

        payload = arguments_result.data
        if not isinstance(payload, dict):
            raise TypeError(f"[{self.name}] tool_arguments_agent returned invalid payload type: {type(payload)}")

        normalized_target = payload.get("target_name")
        normalized_arguments = payload.get("arguments")

        if not isinstance(normalized_target, str) or not normalized_target.strip():
            raise ValueError(f"[{self.name}] tool_arguments_agent payload missing non-empty 'target_name'.")
        if normalized_target.strip() != str(tool).strip():
            raise ValueError(
                f"[{self.name}] tool_arguments_agent target mismatch: expected '{tool}', got '{normalized_target}'."
            )
        if not isinstance(normalized_arguments, dict):
            raise TypeError(
                f"[{self.name}] tool_arguments_agent payload must include dict 'arguments', got {type(normalized_arguments)}."
            )

        return {
            "target_name": normalized_target.strip(),
            "arguments": normalized_arguments,
        }

    def _planner_tool_args_fast_path_enabled(self) -> bool:
        """
        Kill-switch for the Planner -> ToolCaller fast path.

        On by default. Set blackboard ``planner_tool_args_fast_path_enabled``
        to a falsy value to force the slow path (args agent always runs).
        """
        raw = self.blackboard.get_state_value("planner_tool_args_fast_path_enabled", True)
        return bool(raw)

    def _planner_arguments_satisfy_tool_form(self, *, target_name, arguments) -> bool:
        """
        Deterministic precheck for the ToolArguments bypass.

        Contract:
        - target_name must resolve to a known tool.
        - arguments must already satisfy the tool's Pydantic form envelope.

        Returns True iff the planner's args can go straight to the tool
        without an args-agent normalization pass.
        """
        if not self._planner_tool_args_fast_path_enabled():
            return False
        if not isinstance(target_name, str) or not target_name.strip():
            return False
        if not isinstance(arguments, dict):
            return False
        if self.tool_registry.get_tool(target_name) is None:
            return False

        tool_form = self.tool_registry.get_tool_form(target_name)
        if tool_form is None or not hasattr(tool_form, "model_validate"):
            return False

        try:
            tool_form.model_validate(
                {
                    "tool_name": target_name.strip(),
                    "arguments": arguments,
                }
            )
            return True
        except Exception as e:
            logger.debug(
                "[%s] Fast-path precheck rejected planner arguments for target '%s': %s",
                self.name,
                target_name,
                e,
                exc_info=True,
            )
            return False