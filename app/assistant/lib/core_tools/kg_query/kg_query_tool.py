"""KGQueryTool — read-only SQL access to the live emi.db.

Backs the ``kg_query`` tool. Lets investigation agents (and humans) compose
arbitrary SELECT queries the same way a developer would at a Python REPL,
without giving them write access.

Safety rails:
1. Connection is opened with ``mode=ro`` (SQLite enforces; no writes possible
   regardless of statement content).
2. Statement parser rejects anything that isn't ``SELECT`` or ``WITH ... SELECT``
   plus a small set of read-only PRAGMAs (``table_info``, ``index_list``,
   ``index_info``, ``foreign_key_list``, ``table_list``).
3. One statement per call (no semicolon-separated multi-statements).
   Comment-stripping and statement-splitting are string-literal aware: a
   ``;`` or ``--`` inside a quoted string does not get interpreted as
   syntax.
4. Row cap (default 200, max 5000) and per-call timeout (5s) keep accidental
   table scans from hanging the agent loop.

Result shape:
- ``content`` is the rendered pipe-table for the LLM to read inline.
- ``data.columns`` is the column-name list, in order.
- ``data.rows`` is a list of lists (positional). Use this when consuming
  rows programmatically — duplicate column names (``SELECT a.id, b.id ...``)
  preserve both values, which a dict shape would silently collapse.
- ``data.row_dicts`` is the convenience dict shape with collisions
  resolved by suffixing (``id``, ``id_2``, …). Lossless but renames.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Tuple

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

logger = get_logger(__name__)


DEFAULT_MAX_ROWS = 200
ABSOLUTE_MAX_ROWS = 5000
QUERY_TIMEOUT_SECONDS = 5.0

# Statements that are allowed. All others are rejected before reaching SQLite.
_ALLOWED_LEADING_KEYWORDS = ("select", "with")

# Read-only PRAGMAs the agent may want for schema discovery. Anything not in
# this allowlist is rejected even though the connection is read-only — keeps
# the surface tight.
_ALLOWED_PRAGMAS = frozenset({
    "table_info",
    "index_list",
    "index_info",
    "foreign_key_list",
    "table_list",
})

_PRAGMA_RE = re.compile(r"^\s*pragma\s+([a-z_]+)\s*\(", re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────
# String-literal-aware SQL pre-processing
# ──────────────────────────────────────────────────────────────────────────
#
# Naïve regex stripping (`/\*.*?\*/`, `--[^\n]*`, plain `.partition(";")`)
# corrupts queries that contain `--`, `/*`, or `;` inside string literals
# (e.g. `SELECT 'foo--bar'`). The walker below maintains state across:
#
#   - 'single-quoted'   strings, with '' escape
#   - "double-quoted"   identifiers / strings, with "" escape
#   - --line comments   (until newline)
#   - /* block */       comments (until matching `*/`)
#
# It produces a cleaned SQL (with comments replaced by spaces) and the
# index of the first unquoted `;` (or len(sql) if none).


def _scan_sql(sql: str) -> Tuple[str, int]:
    """Walk *sql* once, string-literal aware. Returns
    ``(cleaned_sql, first_semicolon_index)``.

    ``cleaned_sql`` has comments replaced with single spaces (preserves
    column offsets isn't needed; spaces are fine for downstream string
    ops). String contents are passed through verbatim.

    ``first_semicolon_index`` is the position in *sql* (the original,
    pre-clean) of the first unquoted, non-commented `;`, or ``len(sql)``
    if none found.
    """
    out: List[str] = []
    i = 0
    n = len(sql)
    first_semi = n  # default: no semicolon found
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # Single-quoted string: passes through, '' is literal '.
        if ch == "'":
            out.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                out.append(c)
                if c == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Double-quoted identifier/string: passes through, "" is literal ".
        if ch == '"':
            out.append(ch)
            i += 1
            while i < n:
                c = sql[i]
                out.append(c)
                if c == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        out.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Line comment: replace with one space, advance past newline.
        if ch == "-" and nxt == "-":
            out.append(" ")
            i += 2
            while i < n and sql[i] != "\n":
                i += 1
            # leave the newline (if any) intact
            continue

        # Block comment: replace with one space, advance past `*/`.
        if ch == "/" and nxt == "*":
            out.append(" ")
            i += 2
            while i < n - 1 and not (sql[i] == "*" and sql[i + 1] == "/"):
                i += 1
            if i < n - 1:
                i += 2  # skip the `*/`
            else:
                i = n
            continue

        # Unquoted, non-commented semicolon — record first occurrence.
        if ch == ";" and first_semi == n:
            first_semi = i

        out.append(ch)
        i += 1
    return "".join(out), first_semi


def _validate_sql(sql: str) -> None:
    """Raise ValueError if the statement is not a permitted read-only query.

    Connection-level read-only is the real guarantee; this layer rejects
    things early with a clearer error message than SQLite's own.
    """
    if not sql or not sql.strip():
        raise ValueError("sql is empty")

    cleaned, semi_idx = _scan_sql(sql)
    if not cleaned.strip():
        raise ValueError("sql is empty after stripping comments")

    # One statement only. The walker located the first unquoted `;`.
    # Anything non-whitespace after it is a second statement.
    body = cleaned[:semi_idx]
    tail = cleaned[semi_idx + 1:] if semi_idx < len(cleaned) else ""
    if tail.strip():
        raise ValueError(
            "only one statement per call (no semicolon-chained statements)"
        )

    head_token = body.lstrip().split(None, 1)
    head = head_token[0].lower() if head_token else ""

    if head == "pragma":
        m = _PRAGMA_RE.match(body)
        if not m:
            raise ValueError(
                "PRAGMA must be in functional form, e.g. PRAGMA table_info('x')"
            )
        name = m.group(1).lower()
        if name not in _ALLOWED_PRAGMAS:
            raise ValueError(
                f"PRAGMA '{name}' is not allowed. Allowed: {sorted(_ALLOWED_PRAGMAS)}"
            )
        return

    if head not in _ALLOWED_LEADING_KEYWORDS:
        raise ValueError(
            f"statement must start with SELECT or WITH (got '{head}')"
        )


def _resolve_db_path() -> str:
    """Reuse the same DB URI app.models.base computes, then strip the
    sqlite:/// prefix to get a filesystem path for the readonly URI."""
    from app.models.base import get_database_uri
    uri = get_database_uri()
    if uri.startswith("sqlite:///"):
        return uri[len("sqlite:///"):]
    raise RuntimeError(f"kg_query expects a sqlite URI, got: {uri!r}")


def _open_readonly() -> sqlite3.Connection:
    db_path = _resolve_db_path()
    if not os.path.exists(db_path):
        raise RuntimeError(f"database file not found: {db_path}")
    conn = sqlite3.connect(
        f"file:{db_path}?mode=ro",
        uri=True,
        timeout=QUERY_TIMEOUT_SECONDS,
        isolation_level=None,
    )
    conn.row_factory = sqlite3.Row
    # Belt + suspenders: even though mode=ro blocks writes, query_only also
    # blocks ATTACH and a few other surprising paths.
    try:
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.DatabaseError as e:
        # Defense-in-depth only — mode=ro is the real guarantee. Log so
        # the failure surfaces in the rare case it matters.
        logger.debug("[kg_query] PRAGMA query_only failed (defense-in-depth): %s", e)
    return conn


def _format_rows_as_table(columns: List[str], rows: List[List[Any]]) -> str:
    """Render rows as a compact pipe-table the LLM can read at a glance.

    Cells are stringified verbatim — no truncation. Per project policy
    (feedback_no_truncation_tool_results.md) tool content fields must
    not silently elide data. If a row is genuinely too large for a
    single LLM read, that's a query-shape problem, not a renderer
    problem.
    """
    if not rows:
        return "(0 rows)"

    def _cell(v: Any) -> str:
        s = "" if v is None else str(v)
        # Pipes and newlines would break the markdown table layout; replace
        # them with safe forms. This is structural, not truncation.
        return s.replace("|", "/").replace("\n", " ")

    header = " | ".join(columns)
    sep = " | ".join("---" for _ in columns)
    body = "\n".join(" | ".join(_cell(v) for v in row) for row in rows)
    return f"{header}\n{sep}\n{body}"


def _format_summary(row_count: int, truncated: bool, elapsed_ms: int, max_rows: int) -> str:
    parts = [f"{row_count} row(s)", f"{elapsed_ms} ms"]
    if truncated:
        if max_rows >= ABSOLUTE_MAX_ROWS:
            parts.append(
                f"TRUNCATED at {max_rows} (absolute cap; refine the WHERE clause)"
            )
        else:
            parts.append(
                f"TRUNCATED at {max_rows} (raise max_rows up to {ABSOLUTE_MAX_ROWS})"
            )
    return " · ".join(parts)


def _unique_columns(columns: List[str]) -> List[str]:
    """Suffix duplicate column names with _2, _3, ... so the dict shape
    is lossless. Order-preserving."""
    seen: Dict[str, int] = {}
    out: List[str] = []
    for c in columns:
        n = seen.get(c, 0) + 1
        seen[c] = n
        out.append(c if n == 1 else f"{c}_{n}")
    return out


class KGQueryTool(BaseTool):
    """Single read-only SQL query against emi.db.

    Tool name: ``kg_query``. Designed for investigation agents — see
    ``project_kg_finding_resolver_emi_team`` for the broader context.
    """

    def __init__(self) -> None:
        super().__init__("kg_query_tool")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        try:
            arguments = (tool_message.tool_data or {}).get("arguments", {}) or {}
            tool_name = tool_message.tool_name or (tool_message.tool_data or {}).get("tool_name")
            if not tool_name:
                raise ValueError("Missing tool_name in tool_data.")
            handler = getattr(self, f"handle_{tool_name}", None)
            if handler is None:
                raise ValueError(f"Unsupported tool_name for KGQueryTool: {tool_name}")
            return handler(arguments, tool_message)
        except ValueError as e:
            return self.publish_error(make_tool_error(
                error_code="kg_query_invalid",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            ))
        except Exception as e:
            logger.exception("KGQueryTool.execute failed: %s", e)
            return self.publish_error(make_tool_error(
                error_code="kg_query_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=False,
            ))

    def publish_result(self, result: ToolResult) -> ToolResult:
        return result

    def publish_error(self, error_result: ToolResult) -> ToolResult:
        return error_result

    # ---------------------- HANDLERS ----------------------

    def handle_kg_query(self, arguments: Dict[str, Any], tool_message: ToolMessage) -> ToolResult:
        sql = str(arguments.get("sql") or "").strip()
        _validate_sql(sql)

        raw_max = arguments.get("max_rows")
        if raw_max is None:
            max_rows = DEFAULT_MAX_ROWS
        else:
            try:
                max_rows = int(raw_max)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"max_rows must be an integer, got {raw_max!r}"
                ) from exc
        if max_rows < 1:
            max_rows = 1
        if max_rows > ABSOLUTE_MAX_ROWS:
            max_rows = ABSOLUTE_MAX_ROWS

        # Fetch one extra row so we can detect truncation cleanly.
        fetch_limit = max_rows + 1

        logger.debug("[kg_query] sql=%r max_rows=%d", sql, max_rows)
        t0 = time.monotonic()
        conn = _open_readonly()
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            raw_rows = cursor.fetchmany(fetch_limit)
        finally:
            try:
                conn.close()
            except Exception as e:
                logger.debug("[kg_query] connection close failed: %s", e)
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        truncated = len(raw_rows) > max_rows
        if truncated:
            raw_rows = raw_rows[:max_rows]

        # Rows as positional lists — preserves duplicate column names
        # (e.g. SELECT a.id, b.id FROM ...) which a dict shape would
        # silently collapse.
        rows: List[List[Any]] = [list(r) for r in raw_rows]

        # Dict shape is also useful for downstream consumers; rename
        # collisions so it's lossless.
        unique_cols = _unique_columns(columns)
        row_dicts: List[Dict[str, Any]] = [
            dict(zip(unique_cols, row)) for row in rows
        ]

        summary = _format_summary(len(rows), truncated, elapsed_ms, max_rows)
        table = _format_rows_as_table(columns, rows)

        return self.publish_result(ToolResult(
            result_type="kg_query",
            content=summary + "\n\n" + table,
            data={
                "columns": columns,
                "unique_columns": unique_cols,
                "rows": rows,
                "row_dicts": row_dicts,
                "row_count": len(rows),
                "truncated": truncated,
                "elapsed_ms": elapsed_ms,
            },
        ))
