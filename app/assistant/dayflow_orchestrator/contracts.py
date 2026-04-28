"""
Shared Pydantic contracts for inter-node blackboard data in the dayflow orchestrator.

Every control node validates its blackboard inputs through these models at
entry.  This turns silent shape mismatches into loud, immediate failures.

All models use ``extra="allow"`` so source-specific bonus fields don't
cause false failures.  The contracts enforce the *minimum required shape*
that downstream code depends on.
"""

from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Utility — safe metadata extraction
# ---------------------------------------------------------------------------

def get_meta(item: dict[str, Any]) -> dict[str, Any]:
    """Extract metadata dict from a dayflow item, with safe fallback."""
    meta = item.get("metadata")
    return meta if isinstance(meta, dict) else {}


# ---------------------------------------------------------------------------
# Item metadata (the dict inside ``item["metadata"]``)
# ---------------------------------------------------------------------------


class DayflowItemMeta(BaseModel):
    model_config = ConfigDict(extra="allow")

    item_id: str = ""
    state: str = ""
    source_type: str = ""
    event_type: str = ""
    importance: str = "medium"
    actionability: str = ""
    summary: str = ""
    last_reviewed_at: str = ""
    cooldown_until: str | None = None
    plan_id: str = ""
    blocked_by: List[str] = Field(default_factory=list)
    wake_signals: List[str] = Field(default_factory=list)
    reactivate_at_utc: str | None = None
    priority_on_wake: str = ""
    wait_reason: str = ""


# ---------------------------------------------------------------------------
# State mutation (state_mover output → guard → view_materializer → finalize)
# ---------------------------------------------------------------------------


class StateMutationContract(BaseModel):
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    item_id: str
    from_state: str = ""
    to_state: str
    reason: str = ""
    wait_reason: str = ""
    reactivate_at_utc: str = ""
    wake_signals: List[str] = Field(default_factory=list)
    blocked_by: List[str] = Field(default_factory=list)
    priority_on_wake: str = ""


# ---------------------------------------------------------------------------
# Triage artifact decisions (intake_triage output)
# ---------------------------------------------------------------------------


class ArtifactDecisionContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    artifact_id: str
    decision: str
    reason: str = ""
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Planner task (strategic_planner output → state_mover/view/finalize)
# ---------------------------------------------------------------------------


class PlannedTaskContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_id: str
    plan_id: Optional[str] = None
    task: str
    state_instruction: str = ""
    dependencies: List[str] = Field(default_factory=list)
    wait_reason: Optional[str] = None
    reactivate_at: Optional[str] = None
    reactivate_at_utc: Optional[str] = None
    wake_signals: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# View item (view_materializer output → wait_interrupt_promoter → prompt)
# ---------------------------------------------------------------------------


class ViewItemContract(BaseModel):
    model_config = ConfigDict(extra="allow")

    item_id: str
    summary: str = ""
    source_type: str = ""
    event_type: str = ""
    state: str
    importance: str = "medium"
    actionability: str = ""
    wake_signals: List[str] = Field(default_factory=list)
    blocked_by: List[str] = Field(default_factory=list)
    reactivate_at_utc: str = ""
    priority_on_wake: str = ""


# ============================================================================
# Validation helpers — called by control nodes at entry
# ============================================================================


def _validate_list(
    raw: Any,
    model: type[BaseModel],
    label: str,
    *,
    allow_none: bool = True,
) -> None:
    """Validate every element of *raw* through *model*.

    Raises ``ValueError`` with a clear message on the first failure.
    """
    if raw is None and allow_none:
        return
    if not isinstance(raw, list):
        raise ValueError(f"[{label}] expected list, got {type(raw).__name__}.")
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"[{label}][{idx}] expected dict, got {type(item).__name__}.")
        try:
            model.model_validate(item)
        except ValidationError as exc:
            raise ValueError(f"[{label}][{idx}] contract violation: {exc}") from exc


def validate_dayflow_items(raw: Any, label: str) -> None:
    """Validate items that have a ``metadata`` sub-dict (DB / incoming items).

    Each element must be a dict with a ``metadata`` key containing a dict
    that passes :class:`DayflowItemMeta`.
    """
    if raw is None:
        return
    if not isinstance(raw, list):
        raise ValueError(f"[{label}] expected list, got {type(raw).__name__}.")
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"[{label}][{idx}] expected dict, got {type(item).__name__}.")
        meta = item.get("metadata")
        if meta is None:
            continue
        if not isinstance(meta, dict):
            raise ValueError(
                f"[{label}][{idx}] metadata must be dict, got {type(meta).__name__}."
            )
        try:
            DayflowItemMeta.model_validate(meta)
        except ValidationError as exc:
            item_id = meta.get("item_id", f"index_{idx}")
            raise ValueError(
                f"[{label}] item '{item_id}' metadata contract violation: {exc}"
            ) from exc


def validate_mutations(raw: Any, label: str) -> None:
    _validate_list(raw, StateMutationContract, label)


def validate_artifact_decisions(raw: Any, label: str) -> None:
    _validate_list(raw, ArtifactDecisionContract, label)


def validate_planned_tasks(raw: Any, label: str) -> None:
    _validate_list(raw, PlannedTaskContract, label)


def validate_view_items(raw: Any, label: str) -> None:
    _validate_list(raw, ViewItemContract, label)


# ============================================================================
# Short-ID utilities — canonical numeric IDs for LLM-facing prompts
# ============================================================================
#
# Each dayflow item gets a permanent numeric short_id (1, 2, 3, …) stored in
# metadata["short_id"]. Assigned once, never changes, never shared.
# LLM prompts always use the short_id. Resolution back to real item_id is a
# simple metadata lookup.
# ============================================================================


def _find_max_short_id(items: list[dict[str, Any]], *, meta_key: str = "metadata") -> int:
    """Scan items and return the highest existing numeric short_id, or 0."""
    max_id = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get(meta_key) if isinstance(item.get(meta_key), dict) else {}
        raw = meta.get("short_id")
        if raw is None:
            continue
        try:
            val = int(raw)
            if val > max_id:
                max_id = val
        except (ValueError, TypeError):
            continue
    return max_id


def assign_short_ids(
    items: list[dict[str, Any]],
    *,
    meta_key: str = "metadata",
    counter_start: int | None = None,
) -> list[dict[str, Any]]:
    """Assign ``short_id`` to items that don't have one.

    Items that already have a numeric ``short_id`` are left unchanged.
    Items without one get the next available number.

    Returns the list of items that received a new short_id (caller should persist these).
    """
    if counter_start is not None:
        next_id = counter_start
    else:
        next_id = _find_max_short_id(items, meta_key=meta_key) + 1

    newly_assigned: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get(meta_key) if isinstance(item.get(meta_key), dict) else {}

        existing = meta.get("short_id")
        if existing is not None:
            try:
                int(existing)  # valid numeric short_id — keep it
                continue
            except (ValueError, TypeError):
                pass  # invalid — reassign below

        meta["short_id"] = str(next_id)
        next_id += 1
        newly_assigned.append(item)

    return newly_assigned


def resolve_short_id(short_id: str, mapping: dict[str, str] | None = None, items: list | None = None) -> str:
    """Resolve a short ID to its real item_id.

    Checks the mapping dict first (fast path), then scans items if provided.
    Returns the input unchanged if not found.
    """
    sid = str(short_id).strip()
    if mapping and sid in mapping:
        return mapping[sid]
    # Scan items as fallback.
    if items:
        return short_to_long(sid, items)
    return sid


def short_to_long(short_id: str, items: list[dict[str, Any]]) -> str:
    """Find the real item_id for a given short_id by scanning items.

    Returns the short_id unchanged if no match found.
    """
    sid = str(short_id).strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        if str(meta.get("short_id") or "").strip() == sid:
            return str(meta.get("item_id") or item.get("id") or sid).strip()
    return sid


def long_to_short(item_id: str, items: list[dict[str, Any]]) -> str:
    """Find the short_id for a given item_id by scanning items.

    Returns the item_id unchanged if no match found.
    """
    iid = str(item_id).strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = get_meta(item)
        real_id = str(meta.get("item_id") or item.get("id") or "").strip()
        if real_id == iid:
            short = str(meta.get("short_id") or "").strip()
            return short if short else iid
    return iid
