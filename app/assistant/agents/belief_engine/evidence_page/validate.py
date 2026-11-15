"""The evidence page reviewer's rules (agent_runtime/correction.py). Moved from matching.investigation.page_problem
and its correction loop (2026-11-15). The agent_input is the page review_pair makes: `fragments`, each with its
`fragment_id`.

Refused: a fragment of the page with no finding, a finding for an id that is not on the page, a fragment given two
findings, and an empty finding. Full coverage is required: no partial acceptance and no fuzzy id matching (a single
dropped id once aborted the whole global pass, 2026-09-25). An empty finding is corrected finding by finding.
matching.investigation also reads a stored page receipt against these rules.
"""
from app.assistant.agent_runtime.correction import Problem

ITEM_KEYS = {"findings": "fragment_id"}


def validate(answer, agent_input):
    expected = [f["fragment_id"] for f in agent_input["fragments"]]
    findings = answer.get("findings") or []
    actual = [f["fragment_id"] for f in findings]
    out = []
    for fid in expected:
        if fid not in actual:
            out.append(Problem(why="no finding for this fragment; give one finding for every fragment id on the page, "
                               "copied exactly", field="findings", value=fid, structural=True))
    for fid in sorted(set(actual)):
        if fid not in expected:
            out.append(Problem(why="it is not a fragment id on this page; do not return it", field="findings",
                               value=fid, structural=True))
        elif actual.count(fid) > 1:
            out.append(Problem(why="given more than one finding; give one finding per fragment", field="findings",
                               item=fid, structural=True))
    if not out:
        for f in findings:
            if not f["findings"].strip():
                out.append(Problem(why="the finding is empty; say what this fragment shows about the claims",
                                   field="findings", item=f["fragment_id"], at="findings", value=f["findings"]))
    return out
