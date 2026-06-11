from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.frontmatter import split_frontmatter
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root, resolve_repo_path
from app.assistant.utils.time_utils import get_local_time, get_local_timezone

logger = get_logger(__name__)


@dataclass
class TaskSpec:
    task_id: str | None
    manager: str | None
    description: str | None
    task_body: str
    includes: list[str]
    allowed_resources: list[str]
    allowed_read_files: list[str]
    allowed_write_files: list[str]
    frontmatter: dict[str, Any]
    task_includes: str


def _normalize_resource_id(name: str) -> str:
    key = (name or "").strip()
    if not key:
        return ""
    if "@" in key:
        key = key.split("@", 1)[0].strip()
    return key


def _build_resource_scope_context(*, allowed_resource_ids: list[str]) -> dict[str, Any]:
    allowed = [rid for rid in (allowed_resource_ids or []) if isinstance(rid, str) and rid.strip()]
    return {
        "resources": {
            "allowed_global_resources": allowed,
            "allowed_room_resources": [],
            "denied_resources": [],
            "resource_groups": [],
        }
    }


def _format_resource_value_for_include(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Failed JSON-encoding include resource value: %s", e)
        logger.debug("include resource JSON encoding exception details", exc_info=True)
        return str(value)


def _resolve_resource_text(*, resource_id: str, allowed_resource_ids: list[str]) -> str:
    rid = _normalize_resource_id(resource_id)
    if not rid:
        return ""
    resource_manager = getattr(DI, "resource_manager", None)
    if resource_manager is None:
        raise RuntimeError("resource_manager service is not registered.")
    try:
        value = resource_manager.get_resource(
            scope_context=_build_resource_scope_context(allowed_resource_ids=allowed_resource_ids),
            resource_id=rid,
            required=False,
        )
    except Exception as e:
        logger.error("Failed resolving include resource '%s' through scoped resource manager: %s", rid, e)
        logger.debug("include resource scoped resolution exception details", exc_info=True)
        raise
    return _format_resource_value_for_include(value).strip()


def _resolve_include_text(name: str, repo_root: Path, *, allowed_resource_ids: list[str]) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    # Explicit resource id
    if raw.startswith("resource:"):
        return _resolve_resource_text(
            resource_id=raw.split(":", 1)[1].strip(),
            allowed_resource_ids=allowed_resource_ids,
        )

    # File path (relative to repo root or absolute)
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    if candidate.exists() and candidate.is_file():
        try:
            return candidate.read_text(encoding="utf-8")
        except Exception as e:
            logger.error("Failed reading task include file '%s': %s", candidate, e)
            logger.debug("task include file read exception details", exc_info=True)
            raise

    # Bare include token is treated as a resource id.
    return _resolve_resource_text(resource_id=raw, allowed_resource_ids=allowed_resource_ids)


def _normalize_paths(paths: list[str] | None) -> list[str]:
    out: list[str] = []
    for p in paths or []:
        if not isinstance(p, str) or not p.strip():
            continue
        out.append(p.strip())
    return out


def _get_boundary_hour_local(repo_root: Path) -> int:
    """
    Prefer the physical pipeline's boundary hour so tasks can align with the same day semantics.
    Defaults to 5AM local if config is missing.
    """
    try:
        import json

        cfg_path = (repo_root / "configs" / "dayflow_pipeline.json").resolve()
        if not cfg_path.exists():
            return 5
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        daily_reset = cfg.get("daily_reset") or {}
        return int(daily_reset.get("boundary_hour_local", 5))
    except Exception:
        return 5


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    text = (hhmm or "").strip()
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM: {hhmm}")
    return int(parts[0]), int(parts[1])


def _time_between(now_local: datetime, start_hhmm: str, end_hhmm: str) -> bool:
    """
    True if local time is within [start, end) window.
    Supports windows that cross midnight (e.g. 22:00 -> 02:00).
    """
    sh, sm = _parse_hhmm(start_hhmm)
    eh, em = _parse_hhmm(end_hhmm)
    start = (sh, sm)
    end = (eh, em)
    cur = (now_local.hour, now_local.minute)
    if start <= end:
        return start <= cur < end
    # crosses midnight
    return cur >= start or cur < end


def _get_afk_snapshot_best_effort(repo_root: Path) -> dict[str, Any]:
    """
    Best-effort AFK snapshot for task gating.

    Prefer realtime AFKMonitor (fast). Fallback to the latest DayFlow AFK output
    file if monitor is unavailable.
    """
    # 1) Realtime monitor snapshot
    try:
        monitor = getattr(DI, "afk_monitor", None)
        if monitor is not None and hasattr(monitor, "get_computer_activity"):
            snap = monitor.get_computer_activity()
            if isinstance(snap, dict):
                return snap
    except Exception:
        pass

    # 2) Latest pipeline output snapshot (best-effort)
    try:
        import json

        afk_path = (repo_root / "resources" / "dayflow_pipeline_outputs" / "resource_afk_statistics_output.json").resolve()
        if afk_path.exists():
            data = json.loads(afk_path.read_text(encoding="utf-8")) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        pass

    return {}


def _render_task_template(text: str, context: dict[str, Any]) -> str:
    """
    Render task body using a lightweight Jinja2 template.
    If Jinja2 is unavailable or rendering fails, return the original text.
    """
    try:
        from jinja2 import Environment, Undefined

        env = Environment(
            autoescape=False,
            undefined=Undefined,
            keep_trailing_newline=True,
        )
        template = env.from_string(text)
        return template.render(**context)
    except Exception as e:
        logger.debug("Task templating skipped/failed: %s", e, exc_info=True)
        return text


def load_task_spec(path_str: str) -> TaskSpec:
    if not isinstance(path_str, str) or not path_str.strip():
        raise ValueError("task_file must be a non-empty string")
    repo_root = get_repo_root()
    path = resolve_repo_path(path_str.strip())
    if not path.exists():
        raise FileNotFoundError(f"Task spec not found: {path}")
    text = path.read_text(encoding="utf-8")
    frontmatter, body = split_frontmatter(text)

    includes = _normalize_paths(frontmatter.get("includes"))
    allowed_resources = _normalize_paths(frontmatter.get("allowed_resources"))
    inputs = frontmatter.get("inputs") or []
    outputs = frontmatter.get("outputs") or []
    allowed_read_files = _normalize_paths([i.get("path") for i in inputs if isinstance(i, dict)])
    allowed_write_files = _normalize_paths([o.get("path") for o in outputs if isinstance(o, dict)])

    include_resource_ids: list[str] = []
    for inc in includes:
        raw = str(inc or "").strip()
        if not raw:
            continue
        if raw.startswith("resource:"):
            rid = _normalize_resource_id(raw.split(":", 1)[1].strip())
            if rid and rid not in include_resource_ids:
                include_resource_ids.append(rid)
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (repo_root / candidate).resolve()
        if candidate.exists() and candidate.is_file():
            continue
        rid = _normalize_resource_id(raw)
        if rid and rid not in include_resource_ids:
            include_resource_ids.append(rid)

    include_allowed_resources: list[str] = []
    for rid in [*allowed_resources, *include_resource_ids]:
        if isinstance(rid, str) and rid.strip() and rid.strip() not in include_allowed_resources:
            include_allowed_resources.append(rid.strip())

    include_texts: list[str] = []
    for inc in includes:
        content = _resolve_include_text(
            inc,
            repo_root,
            allowed_resource_ids=include_allowed_resources,
        )
        if content:
            include_texts.append(content)
        else:
            logger.warning("Task include not found or empty: %s", inc)
    task_includes = "\n\n".join([t.strip() for t in include_texts if t.strip()])

    # Render task body with time-based conditionals and basic vars.
    now_local = get_local_time()
    boundary_hour = _get_boundary_hour_local(repo_root)
    # Avoid importing timedelta here; compute boundary date using a small trick.
    # (Tasks mostly need time-of-day gating and stable date strings.)
    boundary_date_local = None
    try:
        from datetime import timedelta

        effective = now_local if now_local.hour >= boundary_hour else now_local - timedelta(days=1)
        boundary_date_local = effective.strftime("%Y-%m-%d")
    except Exception:
        boundary_date_local = now_local.strftime("%Y-%m-%d")

    ctx = {
        "now_local": now_local,
        "now_local_str": now_local.strftime("%Y-%m-%d %H:%M:%S"),
        "time_local_hhmm": now_local.strftime("%H:%M"),
        "date_local": now_local.strftime("%Y-%m-%d"),
        "boundary_hour_local": boundary_hour,
        "boundary_date_local": boundary_date_local,
        "timezone": str(getattr(get_local_timezone(), "key", get_local_timezone())),
        "time_between": lambda start, end: _time_between(now_local, str(start), str(end)),
    }

    # AFK gating helpers (best-effort)
    afk_snapshot = _get_afk_snapshot_best_effort(repo_root)
    ctx["afk_snapshot"] = afk_snapshot
    ctx["is_afk"] = bool(afk_snapshot.get("is_afk", False)) if isinstance(afk_snapshot, dict) else False
    ctx["is_potentially_afk"] = (
        bool(afk_snapshot.get("is_potentially_afk", False)) if isinstance(afk_snapshot, dict) else False
    )

    rendered_body = _render_task_template(body.strip(), ctx).strip()

    return TaskSpec(
        task_id=str(frontmatter.get("task_id") or "") or None,
        manager=str(frontmatter.get("manager") or "") or None,
        description=str(frontmatter.get("description") or "") or None,
        task_body=rendered_body,
        includes=includes,
        allowed_resources=allowed_resources,
        allowed_read_files=allowed_read_files,
        allowed_write_files=allowed_write_files,
        frontmatter=frontmatter if isinstance(frontmatter, dict) else {},
        task_includes=task_includes,
    )


def read_compiled_task(path_value: str) -> dict[str, Any]:
    path = resolve_repo_path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Compiled task file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Compiled task file must contain a JSON object")
    return data
