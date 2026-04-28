from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from app.assistant.manager_runtime.services.scope_adapter import build_system_scope_context
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils.pydantic_classes import ScopeContext

logger = get_logger(__name__)


def _pipeline_scope_path(pipeline_id: str) -> Path:
    pid = str(pipeline_id or "").strip()
    if not pid:
        raise ValueError("pipeline_id must be a non-empty string.")
    return get_repo_root() / "app" / "assistant" / "pipelines" / pid / "scope.json"


def load_pipeline_scope_policy(pipeline_id: str) -> Dict[str, Any]:
    path = _pipeline_scope_path(pipeline_id)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Missing required pipeline scope policy file: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed parsing pipeline scope policy '%s': %s", path, e)
        logger.debug("pipeline scope policy parse exception details", exc_info=True)
        raise
    if not isinstance(raw, dict):
        raise ValueError(f"Pipeline scope policy must be a JSON object: {path}")
    resources = raw.get("resources")
    if not isinstance(resources, dict):
        raise ValueError(f"Pipeline scope policy requires 'resources' object: {path}")
    allowed = resources.get("allowed_global_resources")
    if not isinstance(allowed, list):
        raise ValueError(f"Pipeline scope policy requires resources.allowed_global_resources list: {path}")
    denied = resources.get("denied_resources", [])
    if not isinstance(denied, list):
        raise ValueError(f"Pipeline scope policy requires resources.denied_resources list when provided: {path}")
    return raw


def build_pipeline_scope_context(
    *,
    pipeline_id: str,
    actor_id: str,
    room_id: str | None = None,
    room_surface: str | None = None,
) -> ScopeContext:
    policy = load_pipeline_scope_policy(pipeline_id)
    owner_id = str(policy.get("owner_id") or pipeline_id).strip() or pipeline_id
    surface = str(room_surface or policy.get("surface") or "pipeline").strip() or "pipeline"
    base_scope = build_system_scope_context(
        owner_id=owner_id,
        actor_id=str(actor_id or "").strip() or f"{pipeline_id}_runner",
        surface=surface,
        room_id=str(room_id or "").strip() or None,
        room_context_id="main",
        visibility=str(policy.get("visibility") or "owner_only").strip() or "owner_only",
        policy_id=str(policy.get("policy_id") or f"pipeline_policy::{pipeline_id}::v1").strip(),
        scope_id=f"scope::pipeline::{pipeline_id}::{str(actor_id or '').strip() or 'runner'}",
    )
    payload = base_scope.model_dump()
    for section in (
        "history",
        "resources",
        "tools",
        "entities",
        "cards",
        "writes",
        "delivery",
        "approval",
        "retention",
        "execution",
        "delegation",
    ):
        if section in policy:
            value = policy.get(section)
            if not isinstance(value, dict):
                raise ValueError(f"Pipeline scope policy section '{section}' must be an object.")
            payload[section] = value
    return ScopeContext.model_validate(payload)
