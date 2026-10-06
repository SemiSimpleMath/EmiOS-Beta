"""A routine that uses the KG failure ledger tolerates more failed runs than the ledger does (2026-10-06).

The ledger sets an item aside after SET_ASIDE_AFTER failed runs; the run that sets it aside still fails (the item
failed), and the routine manager disables a routine after on_error.max_failures consecutive failed runs (default 3).
With both at 3, one bad proposal disabled the whole promoter on 2026-10-06 02:36, the run before the one that would
have skipped it. Pins: every routine whose code uses the ledger has max_failures above SET_ASIDE_AFTER. The set is
derived from the code (a function routine's handler imports a module that uses the ledger; a pipeline routine's
package does), so a routine that starts using the ledger is caught here.
"""
import ast
import importlib
import inspect
import json
import textwrap
from pathlib import Path

from app.assistant.kg.failure_ledger import SET_ASIDE_AFTER

ROOT = Path(__file__).resolve().parents[4]
LEDGER = "failure_ledger"


def _module_uses_ledger(name: str) -> bool:
    spec = importlib.util.find_spec(name)
    return bool(spec and spec.origin and spec.origin.endswith(".py")
                and LEDGER in Path(spec.origin).read_text(encoding="utf-8"))


def _function_uses_ledger(fn) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(isinstance(n, ast.ImportFrom) and n.module and _module_uses_ledger(n.module)
               for n in ast.walk(tree))


def _pipeline_uses_ledger(pipeline_id: str) -> bool:
    from app.assistant.pipelines.pipeline_registry import resolve_pipeline
    pipeline = resolve_pipeline(pipeline_id)
    package = Path(inspect.getfile(type(pipeline))).parent
    return any(LEDGER in f.read_text(encoding="utf-8") for f in package.rglob("*.py"))


def _routines():
    for f in sorted((ROOT / "configs" / "routines").glob("*/*.json")):
        data = json.loads(f.read_text(encoding="utf-8"))
        if "id" in data:
            yield data


def ledger_routines():
    from app.assistant.routine_manager.routine_functions import ROUTINE_FUNCTION_REGISTRY
    out = {}
    for r in _routines():
        spec = r.get("spec") or {}
        if r.get("runner") == "function" and spec.get("function_name") in ROUTINE_FUNCTION_REGISTRY:
            if _function_uses_ledger(ROUTINE_FUNCTION_REGISTRY[spec["function_name"]]):
                out[r["id"]] = r
        elif r.get("runner") == "pipeline" and spec.get("pipeline_id"):
            if _pipeline_uses_ledger(spec["pipeline_id"]):
                out[r["id"]] = r
    return out


def test_every_ledger_routine_outlasts_the_ledger():
    routines = ledger_routines()
    assert {"proposal_promoter", "kg_pipeline", "kg_maintenance_pipeline"} <= set(routines), sorted(routines)
    short = {rid: (r.get("on_error") or {}).get("max_failures", 3) for rid, r in routines.items()
             if (r.get("on_error") or {}).get("max_failures", 3) <= SET_ASIDE_AFTER}
    assert short == {}, f"max_failures must exceed the ledger's SET_ASIDE_AFTER={SET_ASIDE_AFTER}: {short}"
