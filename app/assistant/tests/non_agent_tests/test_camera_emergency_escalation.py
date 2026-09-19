"""The front-door emergency escalation reaches the user AND reaches dayflow.

Three things had to be true for an emergency at the front door to be handled, and on
2026-09-18 none of them were:

1. The ticket had to be creatable. It was not: the caller passed ticket_kind "info", which
   `_ui_policy_for_ticket_kind` rejects BEFORE the ticket is created. Covered by
   tool_tests/test_ticket_kind_callers_are_valid.py.
2. The ticket had to not hold the camera routine open. It did: the tool's default is to
   block until answered, the camera never reads the answer, and the event routine's soft
   watchdog is 120s — so a 600s block meant a spurious "this routine is stuck" ticket at
   two minutes and the emergency ticket EXPIRING at ten, despite asking for four hours.
3. The frame's pod had to be one dayflow ingests, so the orchestrator could pick the
   emergency up and act on it. It was not: dayflow's allowlist
   (dayflow_orchestrator/ROOM.md, access.ingestion_pod_kinds) asks for
   `ring_doorbell_significant`, and the camera minted `ring_doorbell_event`. Every
   front-door pod was minted and then ignored.

Run:
    .venv\\Scripts\\python.exe -m pytest \\
      app/assistant/tests/non_agent_tests/test_camera_emergency_escalation.py
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import app.assistant.tests.test_setup  # noqa: F401

import pytest

from app.assistant.ring_analysis.camera_dispatcher import _pod_source_kind

_REPO_ROOT = Path(__file__).resolve().parents[4]
_CAMERAS_JSON = _REPO_ROOT / "configs" / "cameras.json"
_ROOM_MD = _REPO_ROOT / "app" / "assistant" / "rooms" / "dayflow_orchestrator" / "ROOM.md"
_CAMERA_DISPATCHER = _REPO_ROOT / "app" / "assistant" / "ring_analysis" / "camera_dispatcher.py"


def _front_door() -> dict:
    cameras = json.loads(_CAMERAS_JSON.read_text(encoding="utf-8"))["cameras"]
    for cam in cameras:
        if str(cam.get("name") or "").strip().lower() == "front door":
            return cam
    raise AssertionError("no camera named 'Front Door' in configs/cameras.json")


def _dayflow_ingested_source_kinds() -> set[str]:
    """The (kind, source_kind) allowlist dayflow ingests, read from ROOM.md frontmatter.

    Parsed rather than hardcoded: the point of the test is that the two sides AGREE, so
    reading one side from a constant would defeat it.
    """
    import yaml

    text = _ROOM_MD.read_text(encoding="utf-8")
    assert text.startswith("---"), "ROOM.md must open with YAML frontmatter"
    frontmatter = text.split("---", 2)[1]
    access = (yaml.safe_load(frontmatter) or {}).get("access") or {}
    entries = access.get("ingestion_pod_kinds") or []
    return {
        str(e.get("source_kind") or "").strip()
        for e in entries
        if isinstance(e, dict) and str(e.get("kind") or "").strip() == "image"
    }


# --------------------------------------------------------------------------- #
# 3. The pod dayflow actually sees
# --------------------------------------------------------------------------- #

def test_the_emergency_pod_kind_is_one_dayflow_ingests():
    """The whole escalation-to-dayflow path rests on these two names matching."""
    policy = _front_door()["pod_policy"]
    emergency_kind = _pod_source_kind(policy, {"category": "emergency"}, camera_id="x")
    assert emergency_kind in _dayflow_ingested_source_kinds(), (
        f"the front door mints {emergency_kind!r} for an emergency, which dayflow does not "
        f"ingest. Allowlist: {sorted(_dayflow_ingested_source_kinds())}"
    )


def test_everyday_categories_stay_out_of_dayflow():
    """A pod per doorbell event would make every passing delivery dayflow intake."""
    policy = _front_door()["pod_policy"]
    for category in ("person", "package", "stranger_lingering", "unknown"):
        kind = _pod_source_kind(policy, {"category": category}, camera_id="x")
        assert kind not in _dayflow_ingested_source_kinds(), (
            f"category {category!r} mints {kind!r}, which dayflow ingests — that is intake "
            f"for an ordinary event"
        )


def test_source_kind_override_only_fires_for_the_named_category():
    policy = {
        "source_kind": "everyday",
        "source_kind_by_category": {"emergency": "escalated"},
    }
    assert _pod_source_kind(policy, {"category": "emergency"}) == "escalated"
    assert _pod_source_kind(policy, {"category": "person"}) == "everyday"
    assert _pod_source_kind(policy, {}) == "everyday", "no category -> the base kind"
    assert _pod_source_kind(policy, {"category": ""}) == "everyday"


def test_source_kind_falls_through_cleanly_when_no_override_is_configured():
    """Every other camera has no override block and must be unaffected."""
    assert _pod_source_kind({"source_kind": "plain"}, {"category": "emergency"}) == "plain"
    assert _pod_source_kind({}, {"category": "x"}, camera_id="77") == "camera_77"
    # A malformed override must not crash the mint.
    assert _pod_source_kind(
        {"source_kind": "plain", "source_kind_by_category": "not a dict"},
        {"category": "emergency"},
    ) == "plain"


# --------------------------------------------------------------------------- #
# 2. The routine is not held open
# --------------------------------------------------------------------------- #

def _ticket_args_in(path: Path) -> list[dict]:
    """Every dict literal in the module that looks like a create_dayflow_ticket arg dict."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        if "ticket_kind" not in keys:
            continue
        literal = {}
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(value, ast.Constant):
                literal[key.value] = value.value
        out.append(literal)
    return out


@pytest.mark.parametrize("rel", [
    "app/assistant/ring_analysis/camera_dispatcher.py",
    "app/assistant/subconscious/scheduler_arbiter_persist.py",
])
def test_fire_and_forget_callers_do_not_block(rel):
    """Neither caller reads the user's reply, so neither may hold a thread waiting for it."""
    dicts = _ticket_args_in(_REPO_ROOT / rel)
    assert dicts, f"{rel}: found no create_dayflow_ticket argument dict — has the call moved?"
    for literal in dicts:
        assert literal.get("wait") is False, (
            f"{rel}: this caller never reads the reply, so it must pass wait=False. "
            f"Blocking tripped the routine watchdog and expired the ticket early."
        )


def test_the_tool_form_can_express_not_waiting():
    """Locked to the default before 2026-09-18: the field did not exist."""
    from app.assistant.lib.tools.create_dayflow_ticket.tool_forms.tool_forms import (
        create_dayflow_ticket_args,
    )

    fields = create_dayflow_ticket_args.model_fields
    assert "wait" in fields
    assert fields["wait"].default is True, "waiting must stay the default for the dayflow lane"
    assert "wait_timeout_seconds" in fields


def test_emergency_is_still_wired_to_both_surfaces():
    """The config this all hangs off. If emergency loses its surfaces, the rest is moot."""
    surfaces = _front_door()["escalation_policy"]["per_category"]["emergency"]
    assert "chat_alert" in surfaces
    assert "dayflow_ticket" in surfaces
