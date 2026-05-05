"""Forward an EmiCode user request to the local Claude Code CLI.

The tool packages a curated skill bundle (CLAUDE.md + matching architecture
docs) with the user's request, shells out to ``claude --print
--output-format stream-json``, parses the result, and returns the final
assistant text. session_id is persisted per room so the next turn can
``--resume`` instead of starting cold.

Read-only by design — the CLI is invoked with ``--allowed-tools
"Read,Glob,Grep"`` so the coding agent can read the repo but cannot edit,
write, or run shell commands. EmiCode's job is to surface proposals; the
user applies changes manually.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.lib.tools.claude_code_invoke import (
    coding_agent_runner,
    session_store,
    skill_curator,
)
from app.assistant.lib.tool_utils.shared_tool_loader import create_tool_loader
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


class ClaudeCodeInvoke(BaseTool):
    """Spawn the user's local ``claude`` CLI with a curated prompt + skills.

    Inputs (per tool_contract.json):
      - task: string — the user's coding request (verbatim from chat_gate).
      - information: string — optional extra context from earlier turns.

    Output: ToolResult.content is the coding agent's final assistant text.
    """

    requires_approval = False  # Read-only — no approval gate needed.

    def __init__(self) -> None:
        super().__init__("claude_code_invoke")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
            task = str(arguments.get("task") or "").strip()
            information = str(arguments.get("information") or "").strip()
            if not task:
                return make_tool_error(
                    error_code="missing_task",
                    message="claude_code_invoke requires a non-empty 'task'.",
                    abort_policy="abort_tool",
                    retryable=True,
                )

            # room_id / room_context_id come in via tool_message.metadata when
            # invoked from a room manager. Fall back to 'emi_code_room' / 'main'
            # so direct (test) invocations still work.
            md = tool_message.metadata or {}
            room_id = str(md.get("room_id") or "emi_code_room").strip() or "emi_code_room"
            room_context_id = str(md.get("room_context_id") or "main").strip() or "main"

            existing_session_id = session_store.get_session_id(room_id, room_context_id)

            # Build the prompt. On a resume we send only the new turn — the
            # CLI carries the prior conversation. On a cold start we prepend
            # the skill bundle so the agent gets oriented in one read.
            if existing_session_id:
                prompt = task
                if information:
                    prompt += f"\n\nAdditional context from EmiCode:\n{information}"
                logger.info(
                    "[claude_code_invoke] resuming session=%s room=%s",
                    existing_session_id, room_id,
                )
            else:
                bundle = skill_curator.assemble_skill_bundle(
                    request=task, info=information,
                )
                if bundle:
                    prompt = (
                        f"{bundle}\n"
                        f"# Request\n\n{task}"
                        + (f"\n\n## Additional context\n\n{information}" if information else "")
                    )
                else:
                    prompt = task
                logger.info(
                    "[claude_code_invoke] cold start room=%s skill_bundle=%d chars",
                    room_id, len(bundle or ""),
                )

            result = coding_agent_runner.run(
                prompt=prompt,
                cwd=get_repo_root(),
                session_id=existing_session_id,
            )

            if not result.success:
                return make_tool_error(
                    error_code="coding_agent_failed",
                    message=result.error or "Coding agent run failed without a specific error.",
                    abort_policy="abort_tool",
                    retryable=True,
                    details={
                        "duration_s": round(result.duration_s, 2),
                        "cli_path": result.cli_path,
                        "session_id": result.session_id,
                    },
                )

            # Persist session_id for the next turn.
            if result.session_id:
                session_store.set_session_id(room_id, room_context_id, result.session_id)

            content = result.final_text or "(coding agent returned no text)"
            return ToolResult(
                result_type="claude_code_invoke",
                content=content,
                data={
                    "session_id": result.session_id,
                    "duration_s": round(result.duration_s, 2),
                    "resumed": bool(existing_session_id),
                    "engine": "claude_code_cli",
                },
            )

        except Exception as exc:
            logger.error("Error in ClaudeCodeInvoke", exc_info=True)
            return make_tool_error(
                error_code="claude_code_invoke_exception",
                message=f"claude_code_invoke crashed: {exc}",
                abort_policy="abort_tool",
                retryable=True,
                details={"exception_type": type(exc).__name__},
            )


get_tool_class = create_tool_loader(ClaudeCodeInvoke)
