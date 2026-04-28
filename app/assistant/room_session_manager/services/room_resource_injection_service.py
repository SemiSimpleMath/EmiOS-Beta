"""
Room resource injection service.

Handles loading resource subscriptions, keyword-based trigger matching,
and building prompt-safe resource context blocks for room agents.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

_SUBSCRIPTIONS_CACHE: dict[str, Any] = {"mtime": None, "size": None, "subscriptions": None}
_SUBSCRIPTIONS_LOCK = threading.Lock()
_TRIGGER_WARNED_IDS: set[str] = set()
_TRIGGER_WARNED_LOCK = threading.Lock()


class RoomResourceInjectionService:
    """Resolves and injects scoped resources into room agent prompts."""

    def __init__(self, *, resource_manager: Any = None):
        self._resource_manager = resource_manager

    # ------------------------------------------------------------------
    # Subscription loading & caching
    # ------------------------------------------------------------------

    @staticmethod
    def _subscriptions_path() -> Path:
        return get_repo_root() / "resources" / "context" / "global" / "resource_subscriptions.json"

    @staticmethod
    def _normalize_subscriptions(raw: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw, dict):
            raise ValueError("resource_subscriptions.json must contain a top-level object.")
        items = raw.get("subscriptions")
        if not isinstance(items, list):
            raise ValueError("resource_subscriptions.json must contain 'subscriptions' as a list.")

        out: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each subscription entry must be an object.")
            resource_id = str(item.get("resource_id") or "").strip()
            if not resource_id:
                raise ValueError("Each subscription entry requires non-empty 'resource_id'.")
            if resource_id in out:
                raise ValueError(f"Duplicate resource subscription for '{resource_id}'.")

            room_eligible = item.get("room_eligible")
            if not isinstance(room_eligible, bool):
                raise ValueError(f"Subscription '{resource_id}' must declare boolean 'room_eligible'.")

            trigger_mode = str(item.get("trigger_mode") or "keyword").strip().lower()
            if trigger_mode not in {"keyword", "rag", "both"}:
                raise ValueError(
                    f"Subscription '{resource_id}' has invalid trigger_mode '{trigger_mode}'. "
                    "Allowed: keyword | rag | both."
                )

            keywords_raw = item.get("keywords")
            if keywords_raw is None:
                keywords_raw = []
            if not isinstance(keywords_raw, list):
                raise ValueError(f"Subscription '{resource_id}' must declare 'keywords' as a list.")
            keywords: list[str] = []
            for k in keywords_raw:
                if not isinstance(k, str) or not k.strip():
                    raise ValueError(f"Subscription '{resource_id}' has invalid keyword entry: {k!r}")
                keywords.append(k.strip().lower())

            always_inject = item.get("always_inject", False)
            if not isinstance(always_inject, bool):
                raise ValueError(f"Subscription '{resource_id}' must declare boolean 'always_inject' when present.")
            templated = item.get("templated", False)
            if not isinstance(templated, bool):
                raise ValueError(f"Subscription '{resource_id}' must declare boolean 'templated' when present.")

            out[resource_id] = {
                "resource_id": resource_id,
                "room_eligible": room_eligible,
                "trigger_mode": trigger_mode,
                "keywords": keywords,
                "always_inject": always_inject,
                "templated": templated,
            }
        return out

    def _load_subscriptions(self) -> dict[str, dict[str, Any]]:
        path = self._subscriptions_path()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Missing required resource subscription file: {path}")
        try:
            stat = path.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except Exception as e:
            logger.error("Failed reading resource subscriptions file metadata: %s", e)
            logger.debug("resource subscriptions stat exception details", exc_info=True)
            raise

        with _SUBSCRIPTIONS_LOCK:
            cached_mtime = _SUBSCRIPTIONS_CACHE.get("mtime")
            cached_size = _SUBSCRIPTIONS_CACHE.get("size")
            cached_subs = _SUBSCRIPTIONS_CACHE.get("subscriptions")
            if cached_mtime == mtime and cached_size == size and isinstance(cached_subs, dict):
                return dict(cached_subs)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            normalized = self._normalize_subscriptions(raw)
        except Exception as e:
            logger.error("Failed loading resource subscriptions from %s: %s", path, e)
            logger.debug("resource subscriptions load exception details", exc_info=True)
            raise

        with _SUBSCRIPTIONS_LOCK:
            _SUBSCRIPTIONS_CACHE["mtime"] = mtime
            _SUBSCRIPTIONS_CACHE["size"] = size
            _SUBSCRIPTIONS_CACHE["subscriptions"] = dict(normalized)
        return dict(normalized)

    # ------------------------------------------------------------------
    # Keyword matching
    # ------------------------------------------------------------------

    @staticmethod
    def _message_matches_keywords(*, inbound_text: str, keywords: list[str]) -> bool:
        text = str(inbound_text or "").strip().lower()
        if not text:
            return False
        for keyword in keywords:
            k = str(keyword or "").strip().lower()
            if not k:
                continue
            if k in text:
                return True
        return False

    # ------------------------------------------------------------------
    # Resource ID selection
    # ------------------------------------------------------------------

    def select_resource_ids_for_injection(
            self,
            *,
            allowed_resource_ids: list[str],
            inbound_text: str,
    ) -> list[str]:
        if not isinstance(allowed_resource_ids, list):
            raise ValueError("allowed_resource_ids must be a list.")

        allowed_ordered: list[str] = []
        for rid in allowed_resource_ids:
            if not isinstance(rid, str) or not rid.strip():
                continue
            cleaned = rid.strip()
            if cleaned not in allowed_ordered:
                allowed_ordered.append(cleaned)
        if not allowed_ordered:
            return []

        subscriptions = self._load_subscriptions()
        subscribed_ids = set(subscriptions.keys())
        selected: list[str] = []

        for rid in allowed_ordered:
            if rid not in subscribed_ids:
                selected.append(rid)

        for rid in allowed_ordered:
            sub = subscriptions.get(rid)
            if not isinstance(sub, dict):
                continue
            if not bool(sub.get("room_eligible", False)):
                continue

            trigger_mode = str(sub.get("trigger_mode") or "keyword").strip().lower()
            if trigger_mode in {"rag", "both"}:
                warn_key = f"{rid}::{trigger_mode}"
                should_warn = False
                with _TRIGGER_WARNED_LOCK:
                    if warn_key not in _TRIGGER_WARNED_IDS:
                        _TRIGGER_WARNED_IDS.add(warn_key)
                        should_warn = True
                if should_warn:
                    logger.warning(
                        "Skipping resource '%s' with unsupported trigger_mode='%s' in room injection.",
                        rid,
                        trigger_mode,
                    )
                continue

            if bool(sub.get("always_inject", False)):
                if rid not in selected:
                    selected.append(rid)
                continue

            keywords = sub.get("keywords") if isinstance(sub.get("keywords"), list) else []
            if self._message_matches_keywords(inbound_text=inbound_text, keywords=keywords):
                if rid not in selected:
                    selected.append(rid)

        return selected

    # ------------------------------------------------------------------
    # Resource resolution & formatting
    # ------------------------------------------------------------------

    def _get_resource_manager(self) -> Any:
        rm = self._resource_manager
        if rm is None:
            from app.assistant.ServiceLocator.service_locator import DI
            rm = getattr(DI, "resource_manager", None)
        if rm is None:
            raise RuntimeError("resource_manager service is not registered.")
        return rm

    def resolve_scoped_resource(self, *, resource_id: str, scope_context: Dict[str, Any]) -> Any:
        rid = str(resource_id or "").strip()
        if not rid:
            return None
        rm = self._get_resource_manager()
        try:
            return rm.get_resource(scope_context=scope_context, resource_id=rid, required=False)
        except Exception as e:
            logger.error("Failed resolving scoped room resource '%s': %s", rid, e)
            logger.debug("scoped room resource resolution exception details", exc_info=True)
            raise

    @staticmethod
    def format_resource_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        try:
            return json.dumps(value, ensure_ascii=False, indent=2).strip()
        except Exception:
            logger.debug("Failed JSON-encoding room resource value for prompt rendering.", exc_info=True)
            return str(value).strip()

    @staticmethod
    def format_daily_context_summary(value: Any) -> str:
        if not isinstance(value, dict):
            return ""

        expected_schedule_value = value.get("expected_schedule")
        expected_schedule = ""
        if isinstance(expected_schedule_value, list):
            schedule_lines: list[str] = []
            for item in expected_schedule_value:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                start_local = str(item.get("start_local") or "").strip()
                end_local = str(item.get("end_local") or "").strip()
                status = str(item.get("status") or "").strip()
                if not title:
                    continue
                if start_local and end_local:
                    time_label = f"{start_local} - {end_local}"
                else:
                    time_label = start_local or end_local
                if time_label and status:
                    schedule_lines.append(f"- {title} ({time_label}) [{status}]")
                elif time_label:
                    schedule_lines.append(f"- {title} ({time_label})")
                elif status:
                    schedule_lines.append(f"- {title} [{status}]")
                else:
                    schedule_lines.append(f"- {title}")
            expected_schedule = "\n".join(schedule_lines).strip()
        elif isinstance(expected_schedule_value, str):
            expected_schedule = expected_schedule_value.strip()
        day_theme = str(value.get("day_theme") or "").strip()
        current_status = str(value.get("current_status") or "").strip()
        milestones = value.get("milestones")
        if not isinstance(milestones, list):
            milestones = []

        has_any = bool(expected_schedule or day_theme or current_status or milestones)
        if not has_any:
            return ""

        lines: list[str] = [
            "## Daily Context",
            "*This captures the main themes and milestones of today - use this to avoid redundant suggestions and understand the day's overall state.*",
        ]

        if expected_schedule:
            lines.append("")
            lines.append(f"**Expected Schedule:** {expected_schedule}")
        if day_theme:
            lines.append("")
            lines.append(f"**Day Theme:** {day_theme}")
        if current_status:
            lines.append("")
            lines.append(f"**Current Status:** {current_status}")
        if milestones:
            lines.append("")
            lines.append("**What's Happened Today:**")
            for milestone in milestones:
                if not isinstance(milestone, dict):
                    continue
                time_val = str(milestone.get("time") or "").strip()
                desc = str(milestone.get("description") or "").strip()
                if not desc:
                    continue
                ongoing = bool(milestone.get("ongoing", False))
                time_prefix = f"[{time_val}] " if time_val else ""
                ongoing_suffix = " (ongoing)" if ongoing else ""
                lines.append(f"- {time_prefix}{desc}{ongoing_suffix}")

        lines.append("")
        lines.append("---")
        return "\n".join(lines).strip()

    def build_allowed_resource_context(
            self,
            allowed_resource_ids: list[str],
            *,
            scope_context: Dict[str, Any],
    ) -> str:
        if not isinstance(allowed_resource_ids, list):
            return ""
        if not isinstance(scope_context, dict):
            raise ValueError("scope_context must be a dict.")

        chunks: list[str] = []
        included_daily_context = False
        for rid in allowed_resource_ids:
            if not isinstance(rid, str) or not rid.strip():
                continue
            resource_id = rid.strip()

            if resource_id in {"resource_daily_context_generator_output", "resource_daily_context"}:
                if included_daily_context:
                    continue
                daily_ctx = self.resolve_scoped_resource(
                    resource_id="resource_daily_context_generator_output",
                    scope_context=scope_context,
                )
                rendered = self.format_daily_context_summary(daily_ctx)
                if rendered:
                    chunks.append(f"## resource_daily_context\n{rendered}")
                    included_daily_context = True
                continue

            value = self.resolve_scoped_resource(resource_id=resource_id, scope_context=scope_context)
            formatted = self.format_resource_value(value)
            if not formatted:
                continue
            chunks.append(f"## {resource_id}\n{formatted}")

        return "\n\n".join(chunks).strip()
