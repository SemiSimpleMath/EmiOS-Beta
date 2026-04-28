"""Dojo run script — run from IDE or terminal."""
import os
import json
from pathlib import Path

os.environ['EMI_BYPASS_APPROVAL'] = '1'

import app.assistant.tests.test_setup  # noqa: F401
from app.assistant.execution_trace.dojo import dojo_run

TASK_ID = 'mcdonalds_two_items'

# Clean Chrome crash state
prefs_path = Path('data/playwright_profile/Default/Preferences')
if prefs_path.exists():
    prefs = json.loads(prefs_path.read_text(encoding='utf-8'))
    if 'profile' in prefs:
        prefs['profile']['exit_type'] = 'Normal'
        prefs['profile']['exited_cleanly'] = True
        prefs_path.write_text(json.dumps(prefs), encoding='utf-8')

# Clean old traces
trace_dir = Path(f'data/execution_traces/{TASK_ID}')
if trace_dir.exists():
    for f in trace_dir.glob('*.json'):
        f.unlink()

# Run
result = dojo_run(TASK_ID, max_runs=3, run_timeout=420, refine=True)
print(json.dumps(result, indent=2, default=str))
