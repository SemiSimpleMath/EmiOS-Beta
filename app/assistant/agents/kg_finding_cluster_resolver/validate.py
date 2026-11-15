"""The finding cluster resolver's rules (agent_runtime/correction.py). Moved from step_cluster_resolve._problems
(2026-11-15). The step gives `finding_ids` (the chunk's finding ids) in agent_input.

Refused, for a "same root" answer: a lead that is not one of the chunk's findings, no members, a member that is not
one of the chunk's findings, or an empty root_question. Siblings are superseded by the lead, so each must be real.
"""
from app.assistant.agent_runtime.correction import Problem


def validate(answer, agent_input):
    if not isinstance(answer.get("is_same_root"), bool):
        return [Problem(why="is_same_root must be true or false", field="is_same_root",
                        value=answer.get("is_same_root"))]
    if not answer["is_same_root"]:
        return []
    ids = set(agent_input["finding_ids"])
    out = []
    if answer.get("lead_finding_id") not in ids:
        out.append(Problem(why="it is not one of the cluster's finding_ids", field="lead_finding_id",
                           value=answer.get("lead_finding_id")))
    members = answer.get("member_finding_ids")
    if not isinstance(members, list) or not members:
        out.append(Problem(why="member_finding_ids is empty; list the findings that share the root",
                           field="member_finding_ids"))
    else:
        foreign = [m for m in members if m not in ids]
        if foreign:
            out.append(Problem(why="these are not among the cluster's finding_ids", field="member_finding_ids",
                               value=foreign))
    if not str(answer.get("root_question") or "").strip():
        out.append(Problem(why="the root_question is empty: write the one question the user would answer",
                           field="root_question"))
    return out
