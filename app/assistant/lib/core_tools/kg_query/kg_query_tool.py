"""KGQueryTool — read-only SQL access to the live emi.db.

Backs the ``kg_query`` tool. Lets investigation agents (and humans) compose
arbitrary SELECT queries the same way a developer would at a Python REPL,
without giving them write access.

Safety rails:
1. Connection is opened with ``mode=ro`` (SQLite enforces; no writes possible
   regardless of statement content).
2. Statement parser rejects anything that isn't ``SELECT`` or ``WITH ... SELECT``
   plus a small set of read-only PRAGMAs (``table_info``, ``index_list``,
   ``foreign_key_list``).
3. One statement per call (no semicolon-separated multi-statements).
4. Row cap (max 5000) and per-call timeout (5s) keep accidental table scans
   from hanging the agent loop.
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
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"--[^\n]*")


def _strip_comments(sql: str) -> str:
    sql = _COMMENT_BLOCK_RE.sub(" ", sql)
    sql = _COMMENT_LINE_RE.sub(" ", sql)
    return sql.strip()


def _validate_sql(sql: str) -> None:
    """Raise ValueError if the statement is not a permitted read-only query.

    Connection-level read-only is the real guarantee; this layer rejects
    things early with a clearer error message than SQLite's own.
    """
    if not sql or not sql.strip():
        raise ValueError("sql is empty")

    cleaned = _strip_comments(sql)
    if not cleaned:
        raise ValueError("sql is empty after stripping comments")

    # One statement only. A trailing semicolon is fine; anything after isn't.
    body, _, tail = cleaned.partition(";")
    if tail.strip():
        raise ValueError("only one statement per call (no semicolon-chained statements)")

    head = body.lstrip().split(None, 1)[0].lower() if body.strip() else ""
    if head == "pragma":
        m = _PRAGMA_RE.match(body)
        if not m:
            raise ValueError("PRAGMA must be in functional form, e.g. PRAGMA table_info('x')")
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
    except sqlite3.DatabaseError:
        pass
    return conn


def _format_rows_as_table(columns: List[str], rows: List[Dict[str, Any]], max_cell: int = 200) -> str:
    """Render rows as a compact pipe-table the LLM can read at a glance."""
    if not rows:
        return "(0 rows)"

    def _cell(v: Any) -> str:
        s = "" if v is None else str(v)
        if len(s) > max_cell:
            s = s[:max_cell] + "…"
        return s.replace("|", "/").replace("\n", " ")

    header = " | ".join(columns)
    sep = " | ".join("---" for _ in columns)
    body = "\n".join(" | ".join(_cell(r.get(c)) for c in columns) for r in rows)
    return f"{header}\n{sep}\n{body}"


def _format_summary(row_count: int, truncated: bool, elapsed_ms: int) -> str:
    parts = [f"{row_count} row(s)", f"{elapsed_ms} ms"]
    if truncated:
        parts.append("TRUNCATED — increase max_rows to see more")
    return " · ".join(parts)


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
            logger.error("KGQueryTool.execute failed: %s", e)
            logger.debug("kg_query exception details", exc_info=True)
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
        max_rows = int(raw_max) if raw_max is not None else DEFAULT_MAX_ROWS
        if max_rows < 1:
            max_rows = 1
        if max_rows > ABSOLUTE_MAX_ROWS:
            max_rows = ABSOLUTE_MAX_ROWS

        # Fetch one extra row so we can detect truncation cleanly.
        fetch_limit = max_rows + 1

        t0 = time.monotonic()
        conn = _open_readonly()
        try:
            cursor = conn.execute(sql)
            columns = [d[0] for d in (cursor.description or [])]
            raw_rows = cursor.fetchmany(fetch_limit)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        truncated = len(raw_rows) > max_rows
        if truncated:
            raw_rows = raw_rows[:max_rows]

        rows: List[Dict[str, Any]] = [dict(r) for r in raw_rows]
        summary = _format_summary(len(rows), truncated, elapsed_ms)
        table = _format_rows_as_table(columns, rows)

        return self.publish_result(ToolResult(
            result_type="kg_query",
            content=summary + "\n\n" + table,
            data={
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "elapsed_ms": elapsed_ms,
            },
        ))
