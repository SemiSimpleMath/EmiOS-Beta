# File: app/assistant/lib/global_tools/base_tool.py

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

from abc import ABC, abstractmethod
from typing import Optional

from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult


class BaseTool(ABC):
    """
    Abstract base class for all tools.

    Tools that need user approval before they execute declare:
        requires_approval = True

    The approval flow itself lives entirely in ApprovalGateway and is
    orchestrated by ToolCaller. BaseTool is only responsible for execution.
    """
    name: str
    requires_approval: bool = False

    def __init__(self, name: str):
        self.name = name

    def compute_approval_reduction(
        self,
        tool_message: ToolMessage,
        caller_authority: int,
    ) -> Optional[int]:
        """Optional per-tool argument-aware approval softening.

        Called by `compute_approval_reasons` before the standard
        authority threshold logic. Tools can override to inspect their
        invocation arguments and decide "in THIS specific case the
        normal approval bar can be lowered."

        Canonical use: send_email returning a lowered threshold when
        the recipient is on an explicit allowlist (so the dayflow
        orchestrator at authority 95 can email family members
        autonomously without firing a confirmation ticket, while a
        send to an arbitrary external address still requires master_room).

        Args:
            tool_message: the invocation, including arguments.
            caller_authority: the calling scope's authority_level,
                passed in so an override can compare without re-reading
                the scope. Implementations should NOT raise authority
                here — only reduce the requirement.

        Returns:
            None  → no softening; standard approval logic applies.
            int   → the effective required authority for THIS specific
                    invocation. If `caller_authority >= return value`,
                    the standard approval logic is bypassed and no
                    ticket fires.

        Implementations must be argument-driven (deterministic given
        the same `tool_message`) and side-effect free. Never read
        from external state that could shift mid-call — load configs
        through cached loaders, not raw filesystem reads each invocation.
        """
        return None

    @abstractmethod
    def execute(self, tool_message: ToolMessage) -> ToolResult:
        """
        Execute the tool. Called after approval has been granted (if required).
        """
        pass

    def describe_action(self, tool_message: ToolMessage) -> tuple[str, str]:
        """
        Return (title, detail) describing what this tool is about to do.

        Override in subclasses to produce a human-readable summary shown to the
        user when approval is required. The default formats the raw arguments as
        JSON. Neither this method nor any subclass override should contain any
        approval logic — that lives entirely in ToolCaller and ApprovalGateway.
        """
        import json
        args = (tool_message.tool_data or {}).get('arguments', {})
        try:
            body = json.dumps(args, indent=2, ensure_ascii=False)
        except Exception:
            body = str(args)
        return (f"Allow {self.name}?", body)
