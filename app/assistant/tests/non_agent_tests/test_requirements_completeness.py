"""Import-completeness: every MODULE-LEVEL third-party import in app/ must be declared
in requirements.txt.

A packaged install installs STRICTLY from requirements.txt, while the dev venv has
extra packages installed "some other way" — so a dev boot can't catch an undeclared
import, but a fresh bundle crashes on it. (This happened: app/me/pagerank.py imports
networkx at module level on the create_app path; it wasn't in requirements.txt →
ModuleNotFoundError on a clean bundle. See scratch/FIX-missing-deps.md.)

Scope/robustness:
- MODULE-LEVEL imports only — those run at import/boot. Lazily-imported optional deps
  (inside functions, e.g. twilio/matplotlib here) are intentionally exempt.
- Flags ONLY packages that ARE installed in the dev env (so map to a real PyPI
  distribution via packages_distributions) yet are missing from requirements.txt.
  → zero false positives: first-party/namespace/stdlib imports are never flagged.
- Test files are skipped (tests aren't on the boot path / in the bundle runtime; e.g.
  pytest is intentionally not a runtime dep).
"""
from __future__ import annotations

import ast
import re
import sys
from importlib import metadata
from pathlib import Path


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "run_flask.py").exists():
            return parent
    raise RuntimeError("repo root not found (run_flask.py marker)")


def _norm(name: str) -> str:
    # PEP 503 normalization for comparing distribution names.
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _requirements_dists(req_path: Path) -> set[str]:
    dists: set[str] = set()
    for line in req_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        line = line.split(" #", 1)[0].strip()  # strip inline comment
        dist = re.split(r"[<>=!~\[;]", line, 1)[0].strip()
        if dist:
            dists.add(_norm(dist))
    return dists


def _module_level_top_imports(tree: ast.Module) -> set[str]:
    """Top-level (module-scope) imported top names. Descends module body + If/Try/With
    (still module scope) but NOT into functions/classes (those imports run lazily)."""
    names: set[str] = set()

    def walk(body):
        for node in body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
            elif isinstance(node, ast.If):
                walk(node.body); walk(node.orelse)
            elif isinstance(node, ast.Try):
                walk(node.body); walk(node.orelse); walk(node.finalbody)
                for h in node.handlers:
                    walk(h.body)
            elif isinstance(node, ast.With):
                walk(node.body)
            # deliberately do NOT descend FunctionDef / AsyncFunctionDef / ClassDef

    walk(tree.body)
    return names


def _is_test_file(path: Path) -> bool:
    parts = set(path.parts)
    return (
        "tests" in parts or "test" in parts
        or path.name.startswith("test_") or path.name == "conftest.py"
    )


def test_app_module_level_imports_are_declared():
    root = _repo_root()
    req = _requirements_dists(root / "requirements.txt")
    pd = metadata.packages_distributions()  # {import_name: [dist_name, ...]}
    stdlib = set(sys.stdlib_module_names)

    violations: dict[str, tuple[list[str], str]] = {}
    for py in (root / "app").rglob("*.py"):
        if _is_test_file(py):
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for top in _module_level_top_imports(tree):
            if top in stdlib or top not in pd:
                continue  # stdlib, or not an installed PyPI dist (first-party/namespace)
            dists = {_norm(d) for d in pd[top]}
            if not (dists & req):
                violations.setdefault(top, (sorted(dists), str(py.relative_to(root))))

    assert not violations, (
        "module-level imports installed in dev but MISSING from requirements.txt "
        "(a fresh bundle install would crash on these):\n"
        + "\n".join(
            f"  import '{name}' -> distribution {dists} (e.g. {where}) — add to requirements.txt"
            for name, (dists, where) in sorted(violations.items())
        )
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
