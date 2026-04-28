from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Protocol


class PipelineStep(Protocol):
    name: str

    def inputs(self, ctx: Any) -> List[str]:
        ...

    def outputs(self, ctx: Any) -> List[Path]:
        ...

    def run(self, ctx: Any) -> None:
        ...


@dataclass
class StepResult:
    name: str
    status: str
    duration_s: float
    error: str | None = None
    inputs: List[str] | None = None
    outputs: List[Path] | None = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "duration_s": round(float(self.duration_s), 2),
        }
        if self.error:
            d["error"] = self.error
        if self.inputs is not None:
            d["inputs"] = list(self.inputs)
        if self.outputs is not None:
            d["outputs"] = [str(p) for p in self.outputs]
        return d


def outputs_exist(step: PipelineStep, ctx: Any) -> bool:
    outs = step.outputs(ctx)
    return bool(outs) and all(p.exists() for p in outs)

