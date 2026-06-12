"""Guardrail: no module uses json.* without importing json (2026-06-12).

The dedup sweep (9906109e) removed `import json` from modules migrated to
parse_jsonish but left direct json.dumps call sites — web_fill_xy and
web_type_focused shipped NameErrors that only fired at tool runtime,
aborting a live DoorDash order days later. AST-scan the whole app tree so
this class can't ship again.
"""
from __future__ import annotations

import ast
import pathlib

from app.assistant.utils.path_utils import get_app_root

ASSISTANT_ROOT = pathlib.Path(get_app_root()) / "assistant"


def test_every_json_user_imports_json():
    offenders = []
    for sub in ("lib", "control_nodes", "kg", "kg_core", "pipelines"):
        for path in (ASSISTANT_ROOT / sub).rglob("*.py"):
            _scan(path, offenders)
    assert not offenders, f"json used without import: {offenders}"


def _scan(path: pathlib.Path, offenders: list) -> None:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    if "json." not in src:
        return
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return

    imports_json = False
    uses = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "json" and (a.asname in (None, "json")) for a in node.names):
                imports_json = True
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "json":
                uses.append(node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            # `json = ...` as a local name makes attribute use legitimate.
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id == "json":
                    imports_json = True

    if uses and not imports_json:
        offenders.append(f"{path.name}:{uses[:3]}")
