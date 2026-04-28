from __future__ import annotations

import threading

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class TaskCreateFinalRouterNode(ControlNode):
    """
    Deterministic router for task-creation mode.

    Every turn:
      - Reads task_spec_markdown from the blackboard and emits a task_draft_update
        WebSocket event so the split-panel preview stays live.
      - Packs chat_response into the final_answer result.

    When task_creation_done_tf is True:
      - Saves the spec markdown to disk.
      - Fires the compile pipeline asynchronously.
      - Emits a task_compiling WebSocket event immediately so the UI can show a spinner.
      - The compile callback emits task_compiled when done.
    """

    # The spec writer agent that runs after the chat agent, just before this router.
    # Accept either the old spec writer or the new spec router node.
    _ACCEPTED_PRIOR_AGENTS = frozenset({"master_room::task_spec_writer", "task_spec_router_node", "task_spec::editor", "task_spec_edit_apply_node"})

    # Emitted on turn 1 when the spec writer no-ops (user typed /task create with no description).
    # Forces the panel open immediately so the user knows where to look.
    _BLANK_SPEC_PLACEHOLDER = "## Title\n[New Task]\n\n## Description\n[TBD]\n\n## Steps\n[No steps defined yet]\n\n## Completion\n[TBD]"

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        final_node = self._required_str(cfg, "final_node")
        chat_response_key = self._optional_str(cfg, "chat_response_key", "chat_response")
        task_done_key = self._optional_str(cfg, "task_done_key", "task_creation_done_tf")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent not in self._ACCEPTED_PRIOR_AGENTS:
            raise ValueError(
                f"[{self.name}] expected last_agent in {self._ACCEPTED_PRIOR_AGENTS!r} before task-create route, got: {last_agent!r}"
            )

        chat_response = str(self.blackboard.get_state_value(chat_response_key, "") or "").strip()
        if not chat_response:
            raise ValueError(
                f"[{self.name}] {chat_response_key!r} must be non-empty for task-create response."
            )

        task_spec_markdown = str(self.blackboard.get_state_value("spec", "") or "").strip()
        task_creation_done_tf = bool(self.blackboard.get_state_value(task_done_key, False))
        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        task_id = str(self.blackboard.get_state_value("task_creation_session_id", "") or "").strip()

        # Emit spec to UI on every turn so the preview stays current.
        if task_spec_markdown and room_id:
            self._emit_draft_update(room_id=room_id, spec_markdown=task_spec_markdown, session_id=task_id)

        logger.info(
            "[%s] task_creation_done_tf=%s task_spec_markdown=%s task_id=%s",
            self.name,
            task_creation_done_tf,
            bool(task_spec_markdown),
            task_id or "(none)",
        )
        # "compile" → enrich with tools first, then compile to IR.
        if task_creation_done_tf and task_spec_markdown:
            precompile_hints = self._pre_compile(
                task_spec_markdown=task_spec_markdown,
            )
            self._fire_compile_async(
                session_id=task_id,
                spec_markdown=task_spec_markdown,
                precompile_hints=precompile_hints,
                room_id=room_id,
            )

        self.blackboard.update_state_value(
            "result",
            {
                "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                "final_answer_answer": chat_response,
                "final_answer_what_was_done": "Task creation mode turn.",
                "final_answer_interesting_info": "",
                "final_answer_sources": [],
                "task_creation_mode_done_tf": task_creation_done_tf,
                "task_spec_markdown": task_spec_markdown,
            },
        )
        self.blackboard.update_state_value("next_agent", final_node)
        self.blackboard.update_state_value("last_agent", self.name)

    def _pre_compile(self, *, task_spec_markdown: str) -> str | None:
        """Pre-compile: send spec to tool planner for executor/tool/dataflow hints.

        Returns the hints markdown (to be written to pre-compile.md by the compile
        runner), or None if the planner produced nothing. Never mutates the spec.
        """
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.lib.task_utils.tool_planner import (
                build_tool_catalog_for_prompt,
                narrow_tools_for_task,
            )
            from app.assistant.utils.pydantic_classes import Message

            # Send the raw spec to the tool planner — it's an LLM, it reads any format.
            narrowed = narrow_tools_for_task(
                task_title="",
                task_goal="",
                step_descriptions=[task_spec_markdown[:500]],
            )
            catalog_text = build_tool_catalog_for_prompt(only_tools=narrowed)

            logger.info("[%s] Pre-compile: sending spec to tool planner.", self.name)

            agent = DI.agent_factory.create_agent("master_room::tool_planner")
            agent.blackboard.update_state_value("agent_input_catalog", catalog_text)
            result = agent.action_handler(Message(agent_input=task_spec_markdown))

            data = result.data or {}
            step_plans = data.get("step_plans") or []

            if step_plans:
                hints = "# Pre-compile hints (machine-generated)\n"
                for plan in step_plans:
                    name = plan.get("step_name", "")
                    kind = plan.get("kind", "")
                    manager = plan.get("manager_name", "")
                    tools = plan.get("tools", [])
                    produces = plan.get("produces", [])
                    consumes = plan.get("consumes", [])
                    hints += f"\n## {name}\n"
                    hints += f"- kind: {kind}\n"
                    if manager:
                        hints += f"- executor: {manager}\n"
                    if tools:
                        for t in tools:
                            hints += f"- tool: {t.get('tool', '')}({t.get('args_json', '{}')})\n"
                    if produces:
                        for p in produces:
                            hints += f"- produces: {p.get('id', '')} ({p.get('description', '')})\n"
                    if consumes:
                        hints += f"- consumes: {', '.join(consumes)}\n"

                logger.info("[%s] Pre-compile: tool planner returned %d step plans.", self.name, len(step_plans))
                return hints

        except Exception as e:
            logger.error("[%s] Pre-compile failed: %s", self.name, e)
            logger.debug("[%s] pre-compile exception", self.name, exc_info=True)

        return None

    def _emit_draft_update(self, *, room_id: str, spec_markdown: str, session_id: str = "") -> None:
        try:
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(
                    widget_data=[
                        {
                            "data_type": "task_draft_update",
                            "spec_markdown": spec_markdown,
                            "session_id": session_id,
                        }
                    ]
                ),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug("[%s] Emitted task_draft_update to room_id %s", self.name, room_id)
        except Exception as e:
            logger.error("[%s] Failed emitting task_draft_update: %s", self.name, e)
            logger.debug("[%s] task_draft_update emit exception", self.name, exc_info=True)

    def _fire_compile_async(self, *, session_id: str, spec_markdown: str, precompile_hints: str | None, room_id: str) -> None:
        if room_id:
            try:
                from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
                assistant_name = get_required_assistant_name()
                msg = UserMessage(
                    data_type="user_msg",
                    sender=assistant_name,
                    receiver=None,
                    role="assistant",
                    metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                    user_message_data=UserMessageData(
                        widget_data=[{"data_type": "task_compiling", "session_id": session_id}]
                    ),
                )
                msg.event_topic = "socket_emit"
                DI.event_hub.publish(msg)
            except Exception as e:
                logger.error("[%s] Failed emitting task_compiling notice: %s", self.name, e)
                logger.debug("[%s] task_compiling emit exception", self.name, exc_info=True)

        thread = threading.Thread(
            target=self._run_compile,
            args=(session_id, spec_markdown, precompile_hints, room_id),
            daemon=True,
            name=f"task-compile-{session_id}",
        )
        thread.start()
        logger.info("[%s] Fired async compile thread for session %s", self.name, session_id)

    @staticmethod
    def _run_compile(session_id: str, spec_markdown: str, precompile_hints: str | None, room_id: str) -> None:
        from app.assistant.lib.task_utils.task_create_compile_runner import TaskCreateCompileRunner
        TaskCreateCompileRunner.run(
            session_id=session_id,
            spec_markdown=spec_markdown,
            precompile_hints=precompile_hints,
            room_id=room_id,
        )

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        task_mode = flow_cfg.get("task_creation_mode")
        if not isinstance(task_mode, dict):
            raise ValueError("manager_flow_config.task_creation_mode must be a dict.")
        return task_mode

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"task create final router requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"task create final router key '{key}' must be non-empty when provided.")
        return raw.strip()
