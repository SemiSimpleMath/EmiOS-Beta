from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class RoutineRunContext:
    run_id: str
    now_utc: datetime
    now_local: datetime
    target_date: Optional[str] = None
    force: bool = False

    # Runner-specific overrides (e.g., pipeline only_steps) can live in routine.spec,
    # but this provides a stable place for cross-cutting flags.


@dataclass(frozen=True)
class RoutineRunResult:
    status: str  # success|error|skipped
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

