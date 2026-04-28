"""Refine-only: analyze the latest trace and update the task spec without re-running."""
import os
import json
from pathlib import Path

os.environ['EMI_BYPASS_APPROVAL'] = '1'

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.execution_trace.dojo import (
    _find_latest_trace, _analyze_trace, _refine_spec, _save_analysis,
    _ensure_baseline_version, _save_spec_version, _get_latest_spec_version,
)

TASK_ID = 'doordash_pizza_hut'

task_dir = Path("tasks") / TASK_ID
task_spec_path = task_dir / "task_spec.md"
task_spec_text = task_spec_path.read_text(encoding="utf-8")

current_version = _ensure_baseline_version(task_dir)
print(f"Current spec version: v{current_version}")

# Find latest trace
trace_path = _find_latest_trace("data/execution_traces", TASK_ID)
if not trace_path:
    print("No trace found.")
    exit(1)

print(f"Using trace: {trace_path.name}")
trace_doc = json.loads(trace_path.read_text(encoding="utf-8"))
print(f"  Steps: {trace_doc.get('total_steps', 0)}, Duration: {trace_doc.get('duration_seconds', 0):.0f}s")

# Analyze
print("\nRunning analysis...")
analysis = _analyze_trace(task_spec_text, trace_doc)
if analysis:
    _save_analysis("data/execution_traces", TASK_ID, current_version, analysis)
    print(f"  Deterministic: {analysis.get('estimated_deterministic_pct', 0):.0f}%")
    print(f"  Suggestions: {len(analysis.get('optimization_suggestions', []))}")
    for s in analysis.get("optimization_suggestions", []):
        print(f"    - {s}")

    # Refine
    print("\nRefining spec...")
    updated_spec = _refine_spec(task_spec_text, analysis, trace_doc)
    if updated_spec:
        new_version = _save_spec_version(task_dir, updated_spec)
        print(f"Spec updated! v{current_version} → v{new_version}")
        print("\n--- Updated spec ---")
        print(updated_spec)
    else:
        print("Refiner returned no changes.")
else:
    print("Analysis failed.")
