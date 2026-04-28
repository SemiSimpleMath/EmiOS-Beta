from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any
import uuid


def make_snapshot_id(tool_name: str) -> str:
    raw = str(tool_name or "snapshot").strip().lower()
    safe = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
    if not safe:
        safe = "snapshot"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    token = uuid.uuid4().hex[:8]
    return f"snap_{safe}_{ts}_{token}"


_ACTIONABLE_ROLES = {
    "button",
    "link",
    "textbox",
    "input",
    "textarea",
    "search",
    "searchbox",
    "checkbox",
    "radio",
    "combobox",
    "menuitem",
    "tab",
    "option",
    "switch",
}

_LINE_WITH_REF_RE = re.compile(
    r'^\s*-\s*(?P<role>[a-zA-Z0-9_<>\-]+)(?:\s+"(?P<label>[^"]{0,220})")?(?P<rest>.*?\[ref=(?P<ref>[^\]]+)\].*)$'
)


def _safe_ascii(text: str, max_len: int = 0) -> str:
    """Normalize text to ASCII. No truncation — data loss is never acceptable."""
    return str(text or "").strip().encode("ascii", "ignore").decode("ascii")


def _extract_page_meta(snapshot_text: str) -> tuple[str | None, str | None]:
    url: str | None = None
    title: str | None = None
    for line in (snapshot_text or "").splitlines():
        s = line.strip()
        if s.startswith("- Page URL:"):
            v = s.split(":", 2)[-1].strip()
            if v:
                url = v
        elif s.startswith("- Page Title:"):
            v = s.split(":", 2)[-1].strip()
            if v:
                title = v
    return (url, title)


def _extract_dialog_subtree(snapshot_text: str) -> str | None:
    """If a dialog/modal node exists in the accessibility tree, return only its subtree.

    The Playwright snapshot uses indentation for nesting. A dialog node looks like:
      - dialog "Modal Title" [ref=eXXX]:
        - button "Close" [ref=eYYY]
        - radio "Option A" [ref=eZZZ]
        ...

    Returns the subtree text (with indentation preserved), or None if no dialog found.
    """
    lines = (snapshot_text or "").splitlines()
    dialog_idx = None
    dialog_indent = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            continue
        # Match dialog role in the accessibility tree
        role_match = re.match(r'^-\s+(dialog|alertdialog)\b', stripped, re.IGNORECASE)
        if role_match:
            dialog_idx = i
            dialog_indent = len(line) - len(stripped)
            break

    if dialog_idx is None:
        return None

    # Collect all lines that are children of the dialog (deeper indentation)
    subtree_lines = [lines[dialog_idx]]
    for i in range(dialog_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= dialog_indent:
            break  # Back to same or higher level — dialog subtree ended
        subtree_lines.append(line)

    return "\n".join(subtree_lines)


def summarize_actionable_snapshot(snapshot_text: str, *, max_elements: int = 50) -> dict[str, Any]:
    """
    Build a compact, deterministic actionable snapshot summary suitable for planner context.
    """
    raw = str(snapshot_text or "")
    url, title = _extract_page_meta(raw)

    # If a dialog/modal is open, only extract elements from inside it.
    dialog_subtree = _extract_dialog_subtree(raw)
    if dialog_subtree:
        raw = dialog_subtree

    items: list[tuple[int, int, dict[str, str]]] = []
    seen_refs: set[str] = set()
    line_idx = 0
    consent_words = ("cookie", "consent", "accept", "reject", "agree", "close", "dismiss", "sign in", "sign up")
    form_roles = {"input", "textarea", "checkbox", "radio", "option"}
    role_base = {
        "input": 145,
        "textarea": 140,
        "checkbox": 130,
        "radio": 130,
        "option": 125,
        "button": 85,
        "menuitem": 75,
        "tab": 70,
        "link": 45,
    }

    for line in raw.splitlines():
        line_idx += 1
        m = _LINE_WITH_REF_RE.match(line.rstrip())
        if not m:
            continue
        role = str(m.group("role") or "").strip().lower()
        # Normalize input-like roles to "input" for clearer planner-facing labels.
        # Keep true textareas as "textarea".
        if role == "textarea":
            role = "textarea"
        elif role in {"input", "textbox", "search", "searchbox", "combobox"}:
            role = "input"
        elif role == "switch":
            role = "checkbox"
        elif ("input" in role) or ("text" in role) or ("search" in role):
            role = "input"
        ref = str(m.group("ref") or "").strip()
        label = _safe_ascii(str(m.group("label") or "").strip(), max_len=140)
        if not ref or ref in seen_refs:
            continue
        if role not in _ACTIONABLE_ROLES:
            continue
        seen_refs.add(ref)
        score = int(role_base.get(role, 50))
        ll = label.lower()
        if any(w in ll for w in consent_words):
            score += 60
        if any(w in ll for w in ("search", "find", "enter", "type", "email", "address", "phone")):
            score += 30
        if "add to cart" in ll or "checkout" in ll or "continue" in ll:
            score += 50
        items.append(
            (
                score,
                line_idx,
                {
                    "ref": ref,
                    "role": role,
                    "text": label or "(no label)",
                },
            )
        )

    items.sort(key=lambda x: (-x[0], x[1]))
    form_items = [it for it in items if it[2].get("role") in form_roles]
    other_items = [it for it in items if it[2].get("role") not in form_roles]

    selected_rows: list[dict[str, str]] = []
    for it in form_items:
        selected_rows.append(it[2])
        if len(selected_rows) >= max_elements:
            break
    if len(selected_rows) < max_elements:
        for it in other_items:
            selected_rows.append(it[2])
            if len(selected_rows) >= max_elements:
                break

    selected = selected_rows
    total = len(items)
    return {
        "url": url,
        "title": title,
        "actionable_elements": selected,
        "actionable_total": total,
        "truncated": total > max_elements,
        "hidden_count": max(0, total - len(selected)),
    }


def has_input_like_elements(elements: list[dict[str, Any]] | None) -> bool:
    if not isinstance(elements, list):
        return False
    for row in elements:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip().lower()
        if role in {"input", "textarea"}:
            return True
    return False


def merge_actionable_with_dom_inputs(
    *,
    elements: list[dict[str, Any]] | None,
    dom_inputs: list[dict[str, Any]] | None,
    max_elements: int = 50,
) -> list[dict[str, Any]]:
    max_elements = max(1, min(int(max_elements or 50), 120))
    base = [x for x in (elements or []) if isinstance(x, dict)]
    dom = [x for x in (dom_inputs or []) if isinstance(x, dict)]
    out: list[dict[str, Any]] = []
    seen_ref: set[str] = set()
    seen_key: set[str] = set()

    def _norm_key(row: dict[str, Any]) -> str:
        role = str(row.get("role") or "").strip().lower()
        text = str(row.get("text") or "").strip().lower()
        x = row.get("x")
        y = row.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return f"{role}|{text}|{int(round(x))}|{int(round(y))}"
        return f"{role}|{text}"

    def _push(row: dict[str, Any]) -> None:
        ref = str(row.get("ref") or "").strip()
        if ref:
            if ref in seen_ref:
                return
            seen_ref.add(ref)
        key = _norm_key(row)
        if key in seen_key:
            return
        seen_key.add(key)
        out.append(row)

    # Put DOM inputs first so planner sees missing input controls.
    for r in dom:
        row = dict(r)
        row["source"] = row.get("source") or "dom"
        if str(row.get("role") or "").strip().lower() not in {"input", "textarea"}:
            row["role"] = "input"
        _push(row)
        if len(out) >= max_elements:
            return out

    for r in base:
        row = dict(r)
        row["source"] = row.get("source") or "mcp"
        _push(row)
        if len(out) >= max_elements:
            return out
    return out


def follow_new_tab_if_opened(
    *,
    server_entry: dict,
    tab_count_before: int,
    mcp_call_fn,
) -> tuple[bool, int | None]:
    """
    After a click, check whether a new tab was opened. If yes, switch to it.

    Returns (switched, new_tab_index).
    `mcp_call_fn(tool_name, arguments)` must call the MCP server and return (text, is_error).
    `tab_count_before` should be the number of tabs recorded just before the click.
    """
    list_text, list_err = mcp_call_fn("browser_tabs", {"action": "list"})
    if list_err or not isinstance(list_text, str):
        return False, None

    # Count tab entries: the playwright-mcp "list" response has one line per tab like:
    #   - 0: Title (active)
    #   - 1: Other Title
    tab_lines = [ln for ln in list_text.splitlines() if re.match(r"\s*-\s*\d+\s*:", ln)]
    tab_count_after = len(tab_lines)

    if tab_count_after <= tab_count_before:
        return False, None

    # A new tab appeared — select the last one (highest index).
    new_index = tab_count_after - 1
    _, sel_err = mcp_call_fn("browser_tabs", {"action": "select", "index": new_index})
    if sel_err:
        return False, None
    return True, new_index

