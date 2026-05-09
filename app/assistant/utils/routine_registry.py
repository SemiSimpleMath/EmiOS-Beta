from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import resolve_repo_path

logger = get_logger(__name__)


@dataclass
class RoutineRegistryEntry:
    routine_id: str
    name: str | None
    runner: str | None
    pipeline_id: str | None
    function_name: str | None
    notes: str | None
    enabled: bool
    aliases: list[str]


def _normalize(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _entry_keys(entry: RoutineRegistryEntry) -> list[str]:
    keys: list[str] = [entry.routine_id]
    if entry.name:
        keys.append(entry.name)
    if entry.pipeline_id:
        keys.append(entry.pipeline_id)
    if entry.function_name:
        keys.append(entry.function_name)
    keys.extend(entry.aliases or [])
    return [_normalize(k) for k in keys if k]


def load_routine_registry(config_path: str | None = None) -> list[RoutineRegistryEntry]:
    """
    Loads routines from the configs/routines/{public,private}/ folders
    (one <id>.json per routine), plus any legacy `routines` array still
    in configs/routines.json. Private overrides public on id collision.

    Supports optional fields: name, aliases.
    """
    settings_path = resolve_repo_path(config_path or "configs/routines.json")
    items: list[Any] = []
    seen_ids: set[str] = set()

    # Public + private folders.
    configs_dir = settings_path.parent
    for folder_name, allow_override in [("public", False), ("private", True)]:
        folder = configs_dir / "routines" / folder_name
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.json")):
            try:
                entry = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("Failed to parse %s: %s", f, e)
                continue
            if not isinstance(entry, dict):
                continue
            rid = str(entry.get("id") or "").strip()
            if not rid:
                continue
            if rid in seen_ids:
                if allow_override:
                    items = [it for it in items if str(it.get("id") or "").strip() != rid]
                else:
                    continue
            items.append(entry)
            seen_ids.add(rid)

    # Legacy monolithic shape: configs/routines.json `routines` array.
    if settings_path.exists():
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("Failed to parse routine config: %s", e)
            cfg = {}
        for entry in cfg.get("routines") or []:
            if not isinstance(entry, dict):
                continue
            rid = str(entry.get("id") or "").strip()
            if rid and rid not in seen_ids:
                items.append(entry)
                seen_ids.add(rid)

    entries: list[RoutineRegistryEntry] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or item.get("routine_id") or "").strip()
        if not rid:
            continue
        name = str(item.get("name") or "").strip() or None
        aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()]
        enabled = bool(item.get("enabled", True))
        runner = str(item.get("runner") or item.get("kind") or "").strip().lower() or None
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        if runner and not spec:
            # Legacy best-effort mapping.
            if runner == "task" and item.get("task_file"):
                spec = {"task_file": item.get("task_file")}
            elif runner == "job" and item.get("job_file"):
                spec = {"job_file": item.get("job_file")}
            elif runner == "tool" and isinstance(item.get("tool_call"), dict):
                spec = {
                    "tool_name": (item.get("tool_call") or {}).get("tool_name"),
                    "arguments": (item.get("tool_call") or {}).get("arguments") or {},
                }
            elif runner == "function" and item.get("function_name"):
                spec = {"function_name": item.get("function_name")}
            elif runner == "pipeline" and item.get("pipeline_id"):
                spec = {"pipeline_id": item.get("pipeline_id")}

        pipeline_id = str(spec.get("pipeline_id") or "").strip() or None
        fn_name = str(spec.get("function_name") or item.get("function_name") or "").strip() or None
        notes = str(item.get("notes") or "").strip() or None
        entries.append(
            RoutineRegistryEntry(
                routine_id=rid,
                name=name,
                runner=runner,
                pipeline_id=pipeline_id,
                function_name=fn_name,
                notes=notes,
                enabled=enabled,
                aliases=aliases,
            )
        )
    return entries


def resolve_routine_entry(
    query: str,
    registry: Iterable[RoutineRegistryEntry] | None = None,
) -> tuple[RoutineRegistryEntry | None, list[RoutineRegistryEntry]]:
    q = _normalize(query)
    if not q:
        return None, []

    entries = list(registry or load_routine_registry())
    if not entries:
        return None, []

    exact_matches = [e for e in entries if q in _entry_keys(e)]
    if len(exact_matches) == 1:
        return exact_matches[0], exact_matches
    if len(exact_matches) > 1:
        return None, exact_matches

    substring_matches: list[RoutineRegistryEntry] = []
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

