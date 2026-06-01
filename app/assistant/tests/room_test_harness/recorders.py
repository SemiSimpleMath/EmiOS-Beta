"""In-memory recorders that capture what would have happened in a real run.

Used by ``simulate_room_inbound`` to observe scope enforcement, tool calls,
LLM calls, and the final outbound — without anything actually reaching the
network or persisted stores.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCallRecord:
    """One observed tool dispatch (whether it ran, blocked, or errored)."""
    tool_name: str
    arguments: Dict[str, Any]
    caller_agent: Optional[str]
    outcome: str  # "ran", "blocked", "error"
    reason: Optional[str] = None  # populated for "blocked" / "error"
    result_summary: Optional[str] = None  # human-readable, for "ran"


@dataclass
class LLMCallRecord:
    agent_name: str
    engine: Optional[str]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float
    duration_ms: int


@dataclass
class HarnessResult:
    """Everything captured from one ``simulate_room_inbound`` call."""
    # The final outbound text the user would have seen (None on no_op / silence).
    final_response: Optional[str]
    # The full agent_input/output trace by agent name (raw manager_result).
    manager_result: Any
    # The ScopeContext dict that was built for this turn.
    scope_dict: Dict[str, Any]
    # Tool calls observed during the turn.
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    # LLM calls observed during the turn (post-mortem from llm_call_log sandbox).
    llm_calls: List[LLMCallRecord] = field(default_factory=list)
    # Wall-clock for the run.
    elapsed_ms: int = 0

    # Convenience accessors --------------------------------------------------

    def tool_calls_by_name(self, tool_name: str) -> List[ToolCallRecord]:
        return [t for t in self.tool_calls if t.tool_name == tool_name]

    def blocked_tool_attempts(self) -> List[ToolCallRecord]:
        return [t for t in self.tool_calls if t.outcome == "blocked"]

    def ran_tools(self) -> List[ToolCallRecord]:
        return [t for t in self.tool_calls if t.outcome == "ran"]

    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.llm_calls)
