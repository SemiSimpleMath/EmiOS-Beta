from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root, resolve_repo_path

logger = get_logger(__name__)


@dataclass
class TaskRegistryEntry:
    task_id: str
    name: str
    task_file: str
    description: str | None
    aliases: list[str]


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _entry_keys(entry: TaskRegistryEntry) -> list[str]:
    keys = [entry.task_id, entry.name]
    keys.extend(entry.aliases or [])
    return [_normalize(k) for k in keys if k]


def _read_frontmatter(path: Path) -> dict[str, Any]:
    """Extract YAML frontmatter from a markdown file. Returns {} if absent or unreadable."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}
    try:
        return yaml.safe_load("\n".join(lines[1:end])) or {}
    except Exception:
        return {}


def _first_heading(path: Path) -> str:
    """Return the text of the first # heading after frontmatter, or ''."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    in_fm = lines and lines[0].strip() == "---"
    fm_end = 0
    if in_fm:
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm_end = i + 1
                break
    for line in lines[fm_end:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _scan_filesystem(repo_root: Path) -> dict[str, TaskRegistryEntry]:
    """
    Scan tasks/*/ for task_spec.md files and build a registry entry for each.

    The task_id is the directory name. Name and description are derived from
    frontmatter (task_id, description keys) and the first # heading.
    """
    tasks_root = repo_root / "tasks"
    entries: dict[str, TaskRegistryEntry] = {}
    if not tasks_root.is_dir():
        return entries

    for child in sorted(tasks_root.iterdir()):
        if not child.is_dir():
            continue
        spec = child / "task_spec.md"
        if not spec.is_file():
            # Also look for any task_spec_*.md in subdirs (e.g. compile_ladder)
            specs = sorted(child.glob("task_spec*.md"))
            for spec in specs:
                fm = _read_frontmatter(spec)
                task_id = str(fm.get("task_id") or "").strip() or spec.stem
                name = _first_heading(spec) or task_id.replace("_", " ")
                description = str(fm.get("description") or "").strip() or None
                rel = spec.relative_to(repo_root).as_posix()
                entries[task_id] = TaskRegistryEntry(
                    task_id=task_id,
                    name=name,
                    task_file=rel,
                    description=description,
                    aliases=[],
                )
            continue

        fm = _read_frontmatter(spec)
        task_id = str(fm.get("task_id") or "").strip() or child.name
        name = _first_heading(spec) or task_id.replace("_", " ")
        description = str(fm.get("description") or "").strip() or None
        rel = spec.relative_to(repo_root).as_posix()
        entries[task_id] = TaskRegistryEntry(
            task_id=task_id,
            name=name,
            task_file=rel,
            description=description,
            aliases=[],
        )

    return entries


def _load_yaml_overrides(path: Path) -> dict[str, dict]:
    """
    Load the optional registry.yaml override file.

    Returns a dict keyed by task_id with the raw YAML entry for that task.
    Only used to enrich filesystem-discovered tasks with human-readable names,
    descriptions, and aliases. Does NOT add tasks that have no task_spec.md.
    """
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("Failed to parse task registry overrides: %s", e)
        return {}
    overrides: dict[str, dict] = {}
    for item in data.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id") or "").strip()
        if tid:
            overrides[tid] = item
    return overrides


def load_task_registry(path: str | None = None) -> list[TaskRegistryEntry]:
    """
    Build the task registry by scanning tasks/*/ for task_spec.md files,
    then enriching with human-readable names/aliases from registry.yaml overrides.

    The YAML file is purely additive metadata — it cannot add tasks that have
    no task_spec.md on disk, and its absence does not break anything.
    """
    repo_root = Path(get_repo_root())
    entries = _scan_filesystem(repo_root)

    registry_path = repo_root / "tasks" / "registry.yaml"
    overrides = _load_yaml_overrides(registry_path)

    for task_id, override in overrides.items():
        if task_id not in entries:
            # Registry references a task_file that may exist at a non-standard path
            task_file = str(override.get("task_file") or "").strip()
            if task_file:
                candidate = repo_root / task_file
                if candidate.is_file():
                    fm = _read_frontmatter(candidate)
                    name = str(override.get("name") or "").strip() or _first_heading(candidate) or task_id.replace("_", " ")
                    description = str(override.get("description") or fm.get("description") or "").strip() or None
                    aliases = [str(a).strip() for a in (override.get("aliases") or []) if str(a).strip()]
                    entries[task_id] = TaskRegistryEntry(
                        task_id=task_id,
                        name=name,
                        task_file=task_file,
                        description=description,
                        aliases=aliases,
                    )
            continue

        entry = entries[task_id]
        if override.get("name"):
            entry.name = str(override["name"]).strip()
        if override.get("description"):
            entry.description = str(override["description"]).strip()
        if override.get("task_file"):
            entry.task_file = str(override["task_file"]).strip()
        aliases = [str(a).strip() for a in (override.get("aliases") or []) if str(a).strip()]
        entry.aliases = aliases

    return list(entries.values())


def resolve_task_entry(query: str, registry: Iterable[TaskRegistryEntry] | None = None) -> tuple[TaskRegistryEntry | None, list[TaskRegistryEntry]]:
    q = _normalize(query)
    if not q:
        return None, []

    entries = list(registry or load_task_registry())
    if not entries:
        return None, []

    exact_matches = [e for e in entries if q in _entry_keys(e)]
    if len(exact_matches) == 1:
        return exact_matches[0], exact_matches
    if len(exact_matches) > 1:
        return None, exact_matches

    substring_matches = []
    for entry in entries:
        for key in _entry_keys(entry):
            if key and (key in q or q in key):
                substring_matches.append(entry)
                break

    if len(substring_matches) == 1:
        return substring_matches[0], substring_matches
    if len(substring_matches) > 1:
        return None, substring_matches

    return None, []
