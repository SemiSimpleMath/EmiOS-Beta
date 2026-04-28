"""
Search-and-replace document editor backed by diff-match-patch.

LLM agents specify old_text / new_text pairs.  This module converts each
pair into a patch via Google's diff-match-patch library and applies it with
fuzzy matching — tolerant of minor whitespace drift, blank-line differences,
and other small mismatches that LLMs introduce.
"""
from __future__ import annotations

import re
from typing import Any, List

from diff_match_patch import diff_match_patch
from pydantic import BaseModel, Field

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_dmp = diff_match_patch()
# How far from the expected location to search (in characters).
# Large value = very tolerant of positional drift.
_dmp.Match_Distance = 10000
# 0.0 = exact match only, 1.0 = match anything. 0.3 is a good default.
_dmp.Match_Threshold = 0.4


class SearchReplaceBlock(BaseModel):
    """A single search-and-replace operation."""
    old_text: str = Field(
        description=(
            "Exact text to find in the document. Must match exactly "
            "(whitespace-sensitive). Include enough surrounding context "
            "to make the match unique."
        ),
    )
    new_text: str = Field(
        description=(
            "Replacement text. Use empty string to delete the matched text."
        ),
    )


class DocumentEdit(BaseModel):
    """Output model for any agent that edits a document."""
    action: str = Field(
        default="no_op",
        description=(
            "Choose 'update' ONLY when the document content must change. "
            "Choose 'no_op' when nothing changed. "
            "When action is 'no_op', edits MUST be empty."
        ),
    )
    edits: List[SearchReplaceBlock] = Field(
        default_factory=list,
        description=(
            "Ordered list of search-and-replace operations to apply. "
            "Each edit finds old_text in the document and replaces it with new_text. "
            "Edits are applied in order, so later edits see the result of earlier ones. "
            "MUST be empty when action is 'no_op'."
        ),
    )


class ApplyResult:
    """Result of applying edits to a document."""

    def __init__(
        self,
        *,
        text: str,
        applied: int,
        failed: int,
        failures: List[str],
    ):
        self.text = text
        self.applied = applied
        self.failed = failed
        self.failures = failures
        self.success = failed == 0

    def __repr__(self) -> str:
        return f"ApplyResult(applied={self.applied}, failed={self.failed})"


def apply_edits(document: str, edits: List[dict[str, Any] | SearchReplaceBlock]) -> ApplyResult:
    """
    Apply a list of search/replace edits to a document using diff-match-patch.

    Each edit's old_text/new_text pair is converted into a patch and applied
    with fuzzy matching.  Edits are applied sequentially.

    Returns an ApplyResult with the patched text, counts, and failure details.
    """
    text = document
    applied = 0
    failed = 0
    failures: List[str] = []

    for i, edit in enumerate(edits):
        if isinstance(edit, dict):
            old_text = str(edit.get("old_text") or "")
            new_text = str(edit.get("new_text") or "")
        else:
            old_text = edit.old_text
            new_text = edit.new_text

        if not old_text:
            if not text.strip() and new_text:
                text = new_text
                applied += 1
                logger.debug("apply_edits: edit[%d] bootstrap — empty doc, inserting.", i)
                continue
            logger.warning("apply_edits: edit[%d] has empty old_text — skipping.", i)
            failed += 1
            failures.append(f"edit[{i}]: empty old_text")
            continue

        if old_text == new_text:
            logger.debug("apply_edits: edit[%d] old_text == new_text — skipping no-op.", i)
            continue

        # 1. Try exact string match first — the common case.
        if old_text in text:
            text = text.replace(old_text, new_text, 1)
            applied += 1
            logger.debug("apply_edits: edit[%d] applied via exact match.", i)
            continue

        # 2. Fuzzy match via diff-match-patch — handles minor whitespace drift.
        patches = _dmp.patch_make(old_text, new_text)
        patched_text, results = _dmp.patch_apply(patches, text)

        if all(results):
            text = patched_text
            applied += 1
            logger.debug(
                "apply_edits: edit[%d] applied via fuzzy patch (%d hunks).",
                i, len(results),
            )
            continue
        if any(results):
            text = patched_text
            applied += 1
            succeeded = sum(results)
            logger.warning(
                "apply_edits: edit[%d] partially applied (%d/%d hunks via fuzzy patch).",
                i, succeeded, len(results),
            )
            continue

        # 3. Whitespace-normalized match. LLMs commonly drop/add blank lines
        # between paragraphs; if the non-blank-line content matches uniquely,
        # locate it in the original and replace the corresponding span.
        located = _locate_whitespace_tolerant(text=text, old_text=old_text)
        if located is not None:
            start, end = located
            text = text[:start] + new_text + text[end:]
            applied += 1
            logger.warning(
                "apply_edits: edit[%d] applied via whitespace-tolerant match (span=%d..%d).",
                i, start, end,
            )
            continue

        preview = old_text[:80].replace("\n", "\\n")
        logger.error(
            "apply_edits: edit[%d] FAILED — no exact, fuzzy, or whitespace-tolerant match. "
            "old_text preview: %r",
            i, preview,
        )
        _log_mismatch_diagnostic(edit_idx=i, old_text=old_text, document=text)
        failed += 1
        failures.append(f"edit[{i}]: no match found: {preview!r}")

    return ApplyResult(
        text=text.strip(),
        applied=applied,
        failed=failed,
        failures=failures,
    )


_WS_RUN_RE = re.compile(r"\s+")


def _locate_whitespace_tolerant(*, text: str, old_text: str) -> tuple[int, int] | None:
    """Find a unique location in `text` whose content matches `old_text` after
    collapsing all whitespace runs (spaces, tabs, newlines) to a single space.
    Returns (start, end) byte offsets in the ORIGINAL `text`, or None if the
    match is ambiguous or absent."""
    # Build a map from offsets in the normalized string back to offsets in the
    # original — each normalized char corresponds to one or more original chars.
    norm_chars: List[str] = []
    orig_starts: List[int] = []  # original start offset of each normalized char
    orig_ends: List[int] = []    # original end offset (exclusive) of each normalized char
    i = 0
    prev_was_space = False
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            if not prev_was_space:
                norm_chars.append(" ")
                orig_starts.append(i)
                # Consume the whole whitespace run.
                j = i
                while j < n and text[j].isspace():
                    j += 1
                orig_ends.append(j)
                i = j
                prev_was_space = True
                continue
            # already recorded a single space — skip additional whitespace
            j = i
            while j < n and text[j].isspace():
                j += 1
            orig_ends[-1] = j
            i = j
        else:
            norm_chars.append(ch)
            orig_starts.append(i)
            orig_ends.append(i + 1)
            i += 1
            prev_was_space = False
    norm_text = "".join(norm_chars)

    # Normalize old_text the same way (collapse any whitespace to a single space).
    norm_old = _WS_RUN_RE.sub(" ", old_text).strip()
    if not norm_old:
        return None

    first = norm_text.find(norm_old)
    if first < 0:
        return None
    # Require uniqueness — otherwise we'd be guessing which occurrence to delete.
    if norm_text.find(norm_old, first + 1) >= 0:
        return None

    start = orig_starts[first]
    end = orig_ends[first + len(norm_old) - 1]
    return start, end


def _log_mismatch_diagnostic(*, edit_idx: int, old_text: str, document: str) -> None:
    """On apply_edits failure, find the longest prefix of old_text that IS in
    the document, then log the bytes around the divergence point in BOTH sides
    so whitespace/unicode mismatches are obvious."""
    # Binary search for the longest prefix of old_text present in document.
    lo, hi = 0, len(old_text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if mid > 0 and old_text[:mid] in document:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    if best == 0:
        logger.error(
            "apply_edits[%d] diagnostic: no prefix of old_text found in document. "
            "doc[0:200]=%r",
            edit_idx, document[:200],
        )
        return

    # Find where the prefix matched in the document.
    prefix = old_text[:best]
    doc_idx = document.find(prefix)
    next_doc_char = document[doc_idx + best : doc_idx + best + 40]
    next_old_char = old_text[best : best + 40]

    logger.error(
        "apply_edits[%d] diverges at char %d of old_text. "
        "matched prefix last 40 chars: %r | expected next (old_text): %r | "
        "actual next (document): %r | old_text_len=%d document_len=%d",
        edit_idx,
        best,
        prefix[-40:],
        next_old_char,
        next_doc_char,
        len(old_text),
        len(document),
    )
    # Also dump codepoints around the divergence — catches invisible unicode
    # differences like smart quotes, nbsp, zero-width, em-dash, etc.
    exp_codes = [hex(ord(c)) for c in next_old_char[:20]]
    got_codes = [hex(ord(c)) for c in next_doc_char[:20]]
    logger.error(
        "apply_edits[%d] codepoints: expected=%s actual=%s",
        edit_idx, exp_codes, got_codes,
    )
