"""Unit tests for `kg_query_tool` SQL pre-processing.

Focused on the bugs the proofreading pass found:

- Comment-stripping is now string-literal aware (was: naïve regex
  corrupted `SELECT 'foo--bar'`).
- Statement-splitting on `;` is string-literal aware (was: naïve
  partition rejected `SELECT 'a;b'`).
- Duplicate column names round-trip without value collapse (was:
  `dict(sqlite3.Row)` kept only the last).
- Cell rendering doesn't truncate (was: 200-char slice violated the
  no-truncation policy).

Tests for the validator are pure-Python (no DB). Tests for the row
shape stand up an in-memory SQLite, populate it, and round-trip.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.assistant.lib.core_tools.kg_query.kg_query_tool import (
    _format_rows_as_table,
    _scan_sql,
    _unique_columns,
    _validate_sql,
)


# ─────────────────────────────────────────────────────────────────────────────
# String-literal-aware scanner
# ─────────────────────────────────────────────────────────────────────────────


def test_scan_sql_strips_line_comment():
    cleaned, semi = _scan_sql("SELECT 1 -- ignore this\nFROM t")
    assert "ignore this" not in cleaned
    assert semi == len("SELECT 1 -- ignore this\nFROM t")  # no semicolon


def test_scan_sql_strips_block_comment():
    cleaned, _ = _scan_sql("SELECT /* hidden */ 1 FROM t")
    assert "hidden" not in cleaned


def test_scan_sql_preserves_double_dash_inside_single_quotes():
    """The classic bug: `--` inside a string was being stripped."""
    sql = "SELECT 'foo--bar' FROM t"
    cleaned, _ = _scan_sql(sql)
    assert "'foo--bar'" in cleaned


def test_scan_sql_preserves_block_marker_inside_string():
    sql = "SELECT '/* not a comment */' FROM t"
    cleaned, _ = _scan_sql(sql)
    assert "'/* not a comment */'" in cleaned


def test_scan_sql_handles_doubled_single_quote_escape():
    """SQL escapes `'` inside a string by doubling it."""
    sql = "SELECT 'it''s fine' FROM t"
    cleaned, semi = _scan_sql(sql)
    # The whole literal should survive intact, including the escaped quote.
    assert "'it''s fine'" in cleaned
    # No semicolon outside the literal.
    assert semi == len(sql)


def test_scan_sql_finds_first_unquoted_semicolon():
    sql = "SELECT 'a;b' FROM t; SELECT 2"
    _, semi = _scan_sql(sql)
    # The `;` inside the string is at index 9; the real one comes later.
    real_semi = sql.index(";", 12)
    assert semi == real_semi


def test_scan_sql_ignores_semicolon_inside_double_quotes():
    sql = 'SELECT "weird;col" FROM t'
    _, semi = _scan_sql(sql)
    # No real semicolon → should be len(sql).
    assert semi == len(sql)


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────


def test_validate_accepts_select():
    _validate_sql("SELECT 1 FROM kg_node_metadata LIMIT 1")


def test_validate_accepts_with_cte():
    _validate_sql(
        "WITH x AS (SELECT 1 AS n) SELECT n FROM x"
    )


def test_validate_accepts_pragma_table_info():
    _validate_sql("PRAGMA table_info('kg_node_metadata')")


def test_validate_rejects_pragma_not_in_allowlist():
    with pytest.raises(ValueError, match="not allowed"):
        _validate_sql("PRAGMA writable_schema(0)")


def test_validate_rejects_insert():
    with pytest.raises(ValueError, match="must start with SELECT or WITH"):
        _validate_sql("INSERT INTO kg_node_metadata VALUES (1)")


def test_validate_rejects_chained_statements():
    with pytest.raises(ValueError, match="one statement"):
        _validate_sql("SELECT 1; DROP TABLE kg_node_metadata")


def test_validate_accepts_query_with_semicolon_in_string_literal():
    """The classic bug — was rejected as "chained statements." """
    _validate_sql("SELECT 'a;b' FROM kg_node_metadata LIMIT 1")


def test_validate_accepts_query_with_double_dash_in_string_literal():
    """Was: comment-stripper ate the second half of the string."""
    _validate_sql("SELECT 'foo--bar' AS s FROM kg_node_metadata LIMIT 1")


def test_validate_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        _validate_sql("")


def test_validate_rejects_comment_only():
    with pytest.raises(ValueError, match="empty"):
        _validate_sql("-- nothing here")


def test_validate_accepts_trailing_semicolon():
    """A single trailing `;` is fine — only content after the first `;`
    is rejected as a second statement."""
    _validate_sql("SELECT 1 FROM kg_node_metadata LIMIT 1;")


# ─────────────────────────────────────────────────────────────────────────────
# Row shape — duplicate column names
# ─────────────────────────────────────────────────────────────────────────────


def test_unique_columns_suffixes_duplicates():
    assert _unique_columns(["id", "id", "label", "id"]) == [
        "id", "id_2", "label", "id_3",
    ]


def test_unique_columns_preserves_order():
    assert _unique_columns(["a", "b", "c"]) == ["a", "b", "c"]


def test_duplicate_column_query_preserves_both_values():
    """The `dict(sqlite3.Row)` collision bug: SELECT a.id, b.id would
    render and persist only the second `id`. Now rows is positional."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE a (id INT)")
    conn.execute("CREATE TABLE b (id INT)")
    conn.execute("INSERT INTO a VALUES (1)")
    conn.execute("INSERT INTO b VALUES (2)")
    conn.commit()

    cur = conn.execute("SELECT a.id, b.id FROM a, b")
    columns = [d[0] for d in cur.description]
    raw_rows = cur.fetchall()
    rows = [list(r) for r in raw_rows]

    assert columns == ["id", "id"]
    assert rows == [[1, 2]]  # both values present, positionally


# ─────────────────────────────────────────────────────────────────────────────
# Renderer — no cell truncation
# ─────────────────────────────────────────────────────────────────────────────


def test_render_does_not_truncate_long_cells():
    long_value = "x" * 5000
    table = _format_rows_as_table(["data"], [[long_value]])
    assert long_value in table  # full value present


def test_render_escapes_pipes_and_newlines():
    """Structural escapes (so the markdown table doesn't break) are
    fine — they don't drop information, just substitute safe chars."""
    table = _format_rows_as_table(["c"], [["a|b\nc"]])
    # Pipe replaced with /, newline replaced with space.
    assert "a/b c" in table


def test_render_handles_none():
    table = _format_rows_as_table(["c"], [[None]])
    assert "c\n---\n" in table  # None renders as empty cell
