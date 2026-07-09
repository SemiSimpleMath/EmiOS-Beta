"""Cache-shape template guards (cache-shape audit C1, 2026-07-09).

The bursty OpenAI agents' user templates opened with the current time —
the one value guaranteed to differ between calls minutes apart — which
ended the provider-cacheable prefix at ~character 54 (state_mover's
static prefix never even reached OpenAI's 1024-token floor). The time
line now renders at the TAIL: same content, position late, so the
stable instructions and burst-stable state blocks join the cached
prefix.
"""
from __future__ import annotations

import pytest

TEMPLATES = [
    "dayflow_orchestrator/strategic_planner_wo/prompts/user.j2",
    "dayflow_orchestrator/work_architect/prompts/user.j2",
    "dayflow_orchestrator/state_mover/prompts/user.j2",
    "dayflow_orchestrator/switchboard/prompts/user.j2",
    "kg_investigation/planner/prompts/user.j2",
    "daily_context_tracker/prompts/user.j2",
    "work_emi_team/planner/prompts/user.j2",
]

TIME_SENTINEL = "TIME-SENTINEL-2026"
BODY_MARKERS = ("TASK-MARKER", "PROJ-MARKER", "HIST-MARKER", "INFO-MARKER")


def _render(rel: str) -> str:
    from app.assistant.agent_runtime.services.prompt_builder import _jinja_env

    template = _jinja_env.get_template(rel)
    return template.render(
        date_time=TIME_SENTINEL,
        day_of_week="Wednesday",
        task="TASK-MARKER",
        information="INFO-MARKER",
        work_projection="PROJ-MARKER",
        recent_history="HIST-MARKER",
        action_count=3,
        # daily_context_tracker renders this through `| tojson`, which
        # cannot serialize an Undefined — give it a real dict.
        current_daily_context={"marker": "PROJ-MARKER"},
    )


@pytest.mark.parametrize("rel", TEMPLATES)
def test_time_still_present_but_at_the_tail(rel):
    out = _render(rel)
    assert TIME_SENTINEL in out, f"{rel}: time lost from the template"
    time_pos = out.rindex(TIME_SENTINEL)
    # Not the head — the old convention put the time in the FIRST line.
    # (Blank-context renders can be tiny, so assert on the first line
    # rather than an absolute offset.)
    first_line = out.lstrip().splitlines()[0] if out.strip() else ""
    assert TIME_SENTINEL not in first_line, f"{rel}: time still renders in the first line"
    # …and after every body block the template carries (task/projection/
    # history/information) — the burst-stable content now precedes it.
    for marker in BODY_MARKERS:
        if marker in out:
            assert time_pos > out.index(marker), (
                f"{rel}: time renders before body marker {marker!r}"
            )
