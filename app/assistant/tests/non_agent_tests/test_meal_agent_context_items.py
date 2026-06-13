"""Meal agents must DECLARE the context vars their templates reference
(regression, 2026-06-13).

daily_meal_proposer and weekly_meal_planner render {{ easy_meals_rotation }}
(the go-to rotation with DUE/RESTING cadence markers) but didn't declare it in
user_context_items, so it rendered EMPTY — the model was told to prefer DUE and
avoid RESTING dishes over content that wasn't there. The value is built in
meal_context_builder alongside the working keys; declaring it is the fix.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from app.assistant.utils.path_utils import get_repo_root

_AGENTS = get_repo_root() / "app" / "assistant" / "agents"


def _check(agent_dir: str):
    base = _AGENTS / agent_dir
    cfg = yaml.safe_load((base / "config.yaml").read_text(encoding="utf-8"))
    user_template = (base / "prompts" / "user.j2").read_text(encoding="utf-8")
    declared = set(cfg.get("user_context_items") or [])
    # If the template renders it, the config must declare it (or it renders blank).
    if "easy_meals_rotation" in user_template:
        assert "easy_meals_rotation" in declared, (
            f"{agent_dir}: user.j2 references easy_meals_rotation but config.yaml "
            f"user_context_items does not declare it -> renders empty"
        )


def test_daily_meal_proposer_declares_easy_meals_rotation():
    _check("daily_meal_proposer")


def test_weekly_meal_planner_declares_easy_meals_rotation():
    _check("weekly_meal_planner")
