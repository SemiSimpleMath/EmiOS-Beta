from __future__ import annotations

from typing import Any, Dict, Optional, Protocol


class RoutineLike(Protocol):
    routine_id: str
    runner: str
    spec: Dict[str, Any]
    notes: Optional[str]
