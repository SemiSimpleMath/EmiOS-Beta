from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root, resolve_repo_path
from app.assistant.utils.task_spec_loader import load_task_spec, TaskSpec

logger = get_logger(__name__)


@dataclass
class JobTaskSpec:
    job_id: str
    manager: str
    task_file: str
    depends_on: list[str]
    information: str
    success_criteria: str
    budget: dict[str, Any]
    task_spec: TaskSpec


@dataclass
class JobSpec:
    job_id: str | None
    description: str | None
    global_context: dict[str, Any]
    tasks: list[JobTaskSpec]
    frontmatter: dict[str, Any]
    job_bundle_text: str


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    raw = text.lstrip("\ufeff")
    if not raw.startswith("---"):
        return {}, text
    parts = raw.split("\n", 1)
    if len(parts) < 2:
        return {}, text
    rest = parts[1]
    if "\n---" not in rest:
        return {}, text
    fm_text, body = rest.split("\n---", 1)
    try:
        frontmatter = yaml.safe_load(fm_text) or {}
    except Exception as e:
        logger.warning("Failed to parse job frontmatter: %s", e)
        frontmatter = {}
    if body.startswith("\n"):
        body = body[1:]
    return frontmatter, body


def _normalize_list(items: list[str] | None) -> list[str]:
    out: list[str] = []
    for item in items or []:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def load_job_spec(path_str: str) -> JobSpec:
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("job_file must be a non-empty string")
    repo_root = get_repo_root()
    path = resolve_repo_path(path_str.strip())
    if not path.exists():
        raise FileNotFoundError(f"Job spec not found: {path}")
    text = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)

    tasks_raw = frontmatter.get("tasks") or []
    tasks: list[JobTaskSpec] = []
    for item in tasks_raw:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("job_id") or item.get("id") or "").strip()
        manager = str(item.get("manager") or item.get("manager_type") or "").strip()
        task_file = str(item.get("task_file") or "").strip()
        if not (job_id and manager and task_file):
            continue
        depends_on = _normalize_list(item.get("depends_on"))
        information = str(item.get("information") or "")
        success_criteria = str(item.get("success_criteria") or item.get("done_criteria") or "")
        budget = item.get("budget") if isinstance(item.get("budget"), dict) else {}
        task_spec = load_task_spec(task_file)
        tasks.append(
            JobTaskSpec(
                job_id=job_id,
                manager=manager,
                task_file=task_file,
                depends_on=depends_on,
                information=information,
                success_criteria=success_criteria,
                budget=budget,
                task_spec=task_spec,
            )
        )

    description = str(frontmatter.get("description") or "") or None
    global_context = frontmatter.get("global_context") if isinstance(frontmatter.get("global_context"), dict) else {}
    allowed_managers = frontmatter.get("allowed_managers") if isinstance(frontmatter.get("allowed_managers"), list) else []

    # Build a deterministic job bundle text for the architect.
    lines: list[str] = []
    if description:
        lines.append(f"Job description: {description}")
    if allowed_managers:
        lines.append(f"Allowed managers: {allowed_managers}")
    if body.strip():
        lines.append("Job details:")
        lines.append(body.strip())
    lines.append("Jobs:")
    for t in tasks:
        lines.append(
            f"- job_id: {t.job_id} | manager_type: {t.manager} | task_file: {t.task_file}"
            + (f" | depends_on: {t.depends_on}" if t.depends_on else "")
            + (f" | success_criteria: {t.success_criteria}" if t.success_criteria else "")
        )
    job_bundle_text = "\n".join(lines).strip()

    return JobSpec(
        job_id=str(frontmatter.get("job_id") or "") or None,
        description=description,
        global_context=global_context,
        tasks=tasks,
        frontmatter=frontmatter if isinstance(frontmatter, dict) else {},
        job_bundle_text=job_bundle_text,
    )
