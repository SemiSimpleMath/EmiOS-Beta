from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class ChildBudget:
    max_cycles: int = 30
    timeout_seconds: int = 180


@dataclass(frozen=True)
class SpawnSpec:
    """
    Minimal spawn spec used by the Orchestrator runtime.
    Produced by the architect agent (structured output) and validated by the orchestrator.
    """

    # Stable identifiers / dependencies
    job_id: str
    child_kind: Literal["manager", "orchestrator"]
    child_type: str
    task: str

    depends_on: tuple[str, ...] = ()
    information: str = ""
    inputs: dict[str, Any] | None = None
    budget: ChildBudget = ChildBudget()
    success_criteria: str = ""


@dataclass
class TeamResult:
    job_id: str
    child_id: str
    child_kind: Literal["manager", "orchestrator"]
    child_type: str
    status: Literal["success", "error", "timeout", "cancelled"]
    summary: str = ""
    evidence_ids: list[str] | None = None
    raw: Any | None = None


@dataclass
class FactsState:
    version: int = 0
    facts: dict[str, Any] | None = None

