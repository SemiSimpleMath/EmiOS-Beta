"""The succession judge's rules (agent_runtime/correction.py). Moved from step_role_succession_scan._problems
(2026-11-15).

Refused: a verdict that is not one of the three, a confidence that is not one of the three, and for a succession a
split_date that is not an ISO date or an empty split_basis. A malformed verdict stops the run; it is never written as
a cooldown stamp.
"""
from datetime import date

from app.assistant.agent_runtime.correction import Problem
from app.assistant.pipelines.kg_maintenance_pipeline.step_role_succession_scan import CONFIDENCE_MAP

VERDICTS = {"stable", "succession", "unclear"}


def validate(answer, agent_input):
    v = str(answer.get("verdict") or "").strip().lower()
    out = []
    if v not in VERDICTS:
        out.append(Problem(why=f"the verdict is not one of {sorted(VERDICTS)}", field="verdict",
                           value=answer.get("verdict")))
    conf = str(answer.get("confidence") or "").strip().lower()
    if conf not in CONFIDENCE_MAP:
        out.append(Problem(why=f"the confidence is not one of {sorted(CONFIDENCE_MAP)}", field="confidence",
                           value=answer.get("confidence")))
    if v == "succession":
        try:
            date.fromisoformat(str(answer.get("split_date") or "")[:10])
        except ValueError:
            out.append(Problem(why="the split_date is not an ISO date such as '2024-09-01'", field="split_date",
                               value=answer.get("split_date")))
        if not str(answer.get("split_basis") or "").strip():
            out.append(Problem(why="the split_basis is empty: say which evidence implies the change",
                               field="split_basis"))
    return out
