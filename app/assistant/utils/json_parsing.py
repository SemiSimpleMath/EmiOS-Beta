"""Best-effort extraction of a JSON value from LLM / MCP tool output.

The ONE ``parse_jsonish`` — consolidates nine near-identical copies found
by the 2026-06-10 duplicate audit (web_* tools, playwright nodes, modal
scanners). Tries, in order:

  1. the whole string as JSON
  2. ``raw_decode`` (leading JSON with trailing prose)
  3. a fenced ```json ... ``` (or bare ```) block, with raw_decode retry
  4. every '{' / '[' position as a JSON start (markdown-wrapped payloads)

A leading markdown header line ("### Result") is stripped first. Returns
None when nothing parses — callers treat that as "no payload", so the
union of the old variants' strategies is strictly safe.
"""
from __future__ import annotations

import json
import re
from typing import Any


def parse_jsonish(text: Any) -> Any:
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s:
        return None
    # Strip a leading markdown header line like "### Result\n".
    s = re.sub(r"^#+\s*\w+\s*\n", "", s).strip()

    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        obj, _idx = json.JSONDecoder().raw_decode(s.lstrip())
        return obj
    except Exception:
        pass

    m = re.search(r"```json\s*([\s\S]*?)```", s, flags=re.IGNORECASE)
    if not m:
        m = re.search(r"```\s*([\s\S]*?)```", s)
    if m:
        payload = (m.group(1) or "").strip()
        try:
            return json.loads(payload)
        except Exception:
            try:
                obj, _idx = json.JSONDecoder().raw_decode(payload.lstrip())
                return obj
            except Exception:
                pass

    for start in re.finditer(r"[\{\[]", s):
        try:
            obj, _idx = json.JSONDecoder().raw_decode(s[start.start():].lstrip())
            return obj
        except Exception:
            continue
    return None
