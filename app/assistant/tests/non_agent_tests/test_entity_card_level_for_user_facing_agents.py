"""An agent that writes prose about an entity must see more than its one-line tagline.

2026-09-11: asked what its entity card said about "Friday Night Meats", the main chat
agent answered that it was a weekly Zoom and "your designated time for good food". The
card was correct and it WAS injected — but `master_room::chat_gate` ran at
`entity_card_level: 0`, and L0 renders only the `level_0` section's one-line intro
("the user's weekly family-and-friends Zoom."). The card's summary, one level up, says it is
named after a Bob's Burgers episode and intentionally misspelled. Given one sentence and
the word "Meats", the model supplied the rest.

Level semantics live in `render_v2_card_for_prompt_injection_level`:
  L0 one-liner | L1 name + summary | L2 + fact bullets | L3 + contact | L4 + relationships.

Two guards, neither of which is a restatement of the config:

1. The PRIMARY UI must never see less entity context than the generic room template it was
   copied from. Both values dated to the initial public release and nobody noticed the
   master room was a level behind for months. That comparison is the check that would have
   caught it.
2. Agents that PRODUCE USER-FACING PROSE need at least the summary. Recognition-only agents
   (triage, state promotion, DAG shaping, the technical tool teams) legitimately stay at L0
   — this list is deliberately short, and an agent belongs on it only if its own words reach
   a person.
"""
from __future__ import annotations

import glob
import os

import pytest
import yaml

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
AGENTS = os.path.join(REPO, "app", "assistant", "agents")

# Agents whose OWN WORDS reach a person, so a one-line tagline is not enough to write from.
USER_FACING = {
    "master_room::chat_gate": "speaks to the user in the primary UI",
    "room::chat_gate": "speaks to the user in every other room",
    "dayflow_orchestrator::plan_mode": "converses with the user in plan mode",
    "ticket_builder::composer": "writes the final user-facing ticket text",
    "emi_team::planner": "produces the answer itself when the goal is to give the user one",
    "work_emi_team::planner": "the work-graph twin of emi_team::planner; findings are results",
}

# Renderer keys with fixed level semantics — an agent asking for these is already above L0
# regardless of its entity_card_level, which only steers the `entity_card` key.
FIXED_LEVEL_KEYS = {"entity_summary", "entity_context", "entity_metadata", "entity_key_facts"}


def _configs():
    out = {}
    for path in glob.glob(os.path.join(AGENTS, "**", "config.yaml"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            try:
                cfg = yaml.safe_load(fh) or {}
            except yaml.YAMLError:
                continue
        if isinstance(cfg, dict) and cfg.get("name"):
            out[cfg["name"]] = cfg
    return out


CONFIGS = _configs()


def _level(cfg):
    """The level the agent's `entity_card` key renders at. Absent means the code default."""
    return int(cfg.get("entity_card_level", 1))


def test_the_primary_ui_is_never_poorer_than_the_generic_room():
    master = CONFIGS.get("master_room::chat_gate")
    generic = CONFIGS.get("room::chat_gate")
    assert master and generic, "both chat gates must exist"
    assert _level(master) >= _level(generic), (
        f"master_room::chat_gate renders entity cards at L{_level(master)} while the generic "
        f"room::chat_gate renders at L{_level(generic)}. The primary UI must never see less "
        "about an entity than every other room."
    )


@pytest.mark.parametrize("agent_name", sorted(USER_FACING))
def test_user_facing_agents_get_at_least_the_card_summary(agent_name):
    cfg = CONFIGS.get(agent_name)
    assert cfg is not None, f"{agent_name} not found — update this list if it was renamed"
    requested = [k for k in (cfg.get("user_context_items") or [])
                 if isinstance(k, str) and k.startswith("entity_")]
    if not requested:
        pytest.skip(f"{agent_name} no longer requests entity context")
    if set(requested) & FIXED_LEVEL_KEYS:
        return  # already renders at fixed semantics above L0
    assert _level(cfg) >= 1, (
        f"{agent_name} renders entity cards at L0 (one-line tagline only) but it "
        f"{USER_FACING[agent_name]}. L0 gives it one sentence per entity and it will invent "
        "the rest, which is how the Friday Night Meats card came back as being about food."
    )
