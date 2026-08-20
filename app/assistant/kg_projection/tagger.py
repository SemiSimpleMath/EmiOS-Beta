"""Per-bullet section-classification via LLM.

Takes a list of bullets (plain strings for now; will become ``Bullet``
dataclasses after ``bullets.py`` is extracted) plus the section taxonomy,
and returns a ``{bullet_key: [section_key, ...]}`` map. Content-hash cached
via ``tag_cache``.

The tagger itself is an LLM agent the caller passes in. This module is
agent-agnostic — it does the batching, prompt shaping, result parsing, and
cache logic; the caller owns which agent runs and with what scope.

When the caller provides a ``window_loader`` and per-bullet window ids, the
tagger groups bullets by their source window in the prompt and prefixes each
group with the resolved window text. This grounds tagging in the original
conversational context — see feedback_full_resolved_window_beats_fragments.
With window context on, the per-call batch size is reduced (more text per
call) — the cache still keys by bullet content hash, so repeat runs are
near-free regardless of which mode produced the original tag.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from app.assistant.kg_projection.tag_cache import bullet_key
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message, ScopeContext

logger = get_logger(__name__)

# Default batch size when bullets are sent without window grounding.
TAGGER_BATCH_SIZE = 20

# Smaller batch when each bullet group is prefixed with its source window —
# windows are 1–7 KB each, so we want fewer per call.
TAGGER_BATCH_SIZE_WITH_WINDOWS = 10


def _format_bullets_block(batch: List[str], *, start_number: int = 1) -> str:
    """Flat numbered bullet block for the tagger prompt."""
    return "\n\n".join(
        f"{start_number + i}. {bullet}" for i, bullet in enumerate(batch)
    )


def _format_bullets_block_windowed(
    batch: List[str],
    batch_keys: List[str],
    bullet_windows: Dict[str, List[str]],
    window_loader: Callable[[str], str],
) -> str:
    """Window-grouped bullet block: each unique source window is shown once,
    with the bullets that came from it listed under it. Bullet numbering is
    global across the whole batch (the model's output references those numbers).

    A bullet whose ``source_window_ids`` is empty (or whose windows can't be
    resolved) lands under an "Ungrouped" section at the end.
    """
    # Determine the primary window per bullet (first in source_window_ids).
    # Group bullets by primary window, preserving original order.
    primary_window: List[Optional[str]] = []
    for bkey in batch_keys:
        wids = bullet_windows.get(bkey) or []
        primary_window.append(wids[0] if wids else None)

    # Order windows by first appearance.
    window_order: List[Optional[str]] = []
    for w in primary_window:
        if w not in window_order:
            window_order.append(w)

    # Pre-load each unique window's text once.
    window_text: Dict[str, str] = {}
    for w in window_order:
        if w is None:
            continue
        try:
            txt = (window_loader(w) or "").strip()
        except Exception as e:
            logger.warning("window_loader failed for %s: %s", w, e)
            txt = ""
        window_text[w] = txt

    parts: List[str] = []
    for w in window_order:
        member_indices = [i for i, pw in enumerate(primary_window) if pw == w]
        if not member_indices:
            continue
        if w is not None:
            txt = window_text.get(w) or "(window text unavailable)"
            parts.append(f"=== Conversation context (window {w[:8]}) ===")
            parts.append(txt)
            parts.append("")
            parts.append("Bullets from this window:")
        else:
            parts.append("=== Bullets without resolvable window context ===")
        for i in member_indices:
            parts.append(f"{i + 1}. {batch[i]}")
        parts.append("")

    return "\n".join(parts).rstrip()


def tag_bullets(
    agent,
    *,
    entity_label: str,
    entity_type: str,
    bullets: List[str],
    allowed_sections_block: str,
    allowed_keys: Iterable[str],
    cached_tags: Dict[str, List[str]],
    scope_context: Optional[ScopeContext] = None,
    bullet_windows: Optional[Dict[str, List[str]]] = None,
    window_loader: Optional[Callable[[str], str]] = None,
) -> Dict[str, List[str]]:
    """Classify each bullet into zero-or-more sections. Returns ``{bullet_key: [section_key, ...]}``.

    Uses the cache for unchanged bullets and batches uncached bullets through
    the tagger LLM. Each batch is an independent call — no accumulator state
    flows between batches, so one bad batch doesn't poison the others. A
    batch that raises ABORTS the run (no tags are invented for its bullets —
    an emitted [] would persist in the sidecar as a permanent "no section"
    verdict); bullets a successful batch omits are left uncached for retry.

    When ``bullet_windows`` and ``window_loader`` are both provided, the
    prompt is built window-grouped — each unique source window's resolved
    text is shown once, with the bullets from that window listed under it.
    Batch size drops from ``TAGGER_BATCH_SIZE`` (20) to
    ``TAGGER_BATCH_SIZE_WITH_WINDOWS`` (10) in this mode to keep per-call
    size bounded; the bullet content-hash cache still de-dupes across runs.
    """
    allowed_keys = set(allowed_keys)
    tags: Dict[str, List[str]] = {}
    uncached: list[tuple[str, str]] = []  # [(bullet_key, bullet_text), ...]

    for bullet in bullets:
        key = bullet_key(bullet)
        if key in tags:
            continue
        hit = cached_tags.get(key)
        if hit is not None:
            tags[key] = hit
            continue
        uncached.append((key, bullet))

    if not uncached:
        return tags

    use_windows = bullet_windows is not None and window_loader is not None
    batch_size = TAGGER_BATCH_SIZE_WITH_WINDOWS if use_windows else TAGGER_BATCH_SIZE

    logger.info(
        "Tagging %d new bullets for %s (%d already cached). %s",
        len(uncached), entity_label, len(tags),
        f"(windowed batch size {batch_size})" if use_windows else f"(flat batch size {batch_size})",
    )

    for start in range(0, len(uncached), batch_size):
        batch = uncached[start:start + batch_size]
        batch_keys = [k for k, _ in batch]
        batch_texts = [t for _, t in batch]
        if use_windows:
            bullets_block = _format_bullets_block_windowed(
                batch_texts, batch_keys, bullet_windows, window_loader,
            )
        else:
            bullets_block = _format_bullets_block(batch_texts)
        msg = Message(
            agent_input={
                "entity_name": entity_label,
                "entity_type": entity_type,
                "bullets_block": bullets_block,
                "allowed_sections": allowed_sections_block,
            },
            scope_context=scope_context,
        )
        try:
            result = agent.action_handler(msg)
            data = getattr(result, "data", None) or {}
            rows = data.get("results") or []
            # Index-keyed dict from the agent's 1-based output.
            by_number: Dict[int, List[str]] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    n = int(row.get("number"))
                except (TypeError, ValueError):
                    continue
                raw_sections = row.get("sections") or []
                clean = [str(t).strip() for t in raw_sections if isinstance(t, str)]
                clean = [t for t in clean if t in allowed_keys]
                by_number[n] = clean
            for i, bkey in enumerate(batch_keys, start=1):
                if i not in by_number:
                    # No verdict is NOT a "belongs nowhere" verdict: the map is
                    # persisted as the durable tag sidecar, and a [] written here
                    # would permanently exclude this fact from the wiki. Leaving
                    # the key out keeps it uncached, so the next run retries it.
                    logger.warning(
                        "Tagger omitted bullet %d in batch starting at %d for %s — left uncached",
                        i, start, entity_label,
                    )
                    continue
                tags[bkey] = by_number[i]
        except Exception:
            # A failed batch must not mint verdicts (the old path wrote [] per
            # bullet into the sidecar — a permanent "no section" judgment from an
            # outage). Raise: the page's generation aborts, it stays dirty, and
            # the caller records the error.
            logger.error(
                "Tagger failed for batch starting at %d for %s — aborting page tagging",
                start, entity_label,
            )
            raise

    return tags


def group_bullets_by_section(
    bullets: List[str],
    tags: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Invert bullet → tags map into ``section_key → [bullets]``.

    A bullet may appear in multiple sections if it was tagged multiple ways,
    which is the intended behavior — section slices are allowed to overlap
    at the source level.
    """
    grouped: Dict[str, List[str]] = {}
    for bullet in bullets:
        key = bullet_key(bullet)
        for sec in tags.get(key, []):
            grouped.setdefault(sec, []).append(bullet)
    return grouped
