"""Run explicitly requested model evaluations on a disposable belief database copy.

Example: python -m belief_engine.review.cli --baseline /path/baseline.db
  --working /path/reviewed.db --cases /path/cases.json --output /path/results
  --run-models

cases.json: [{"name": "example", "question": "...", "belief_keys": ["..."]}]
Optional insights JSON: [{"ref": "daily:date:index", "interpretation": {...}}].
The caller is responsible for authorization to send these sources to the configured LLM.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('baseline', 'working', 'cases', 'output'):
        parser.add_argument('--' + arg, required=True, type=Path)
    parser.add_argument('--insights', type=Path)
    parser.add_argument('--run-models', action='store_true')
    args = parser.parse_args()
    if not args.run_models:
        parser.error('Explicit --run-models is required; this sends evidence to the configured LLM')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime_db = output / 'runtime.db'
    if runtime_db in (args.baseline.resolve(), args.working.resolve()):
        parser.error('Runtime database must be distinct from the source and review databases')
    from dotenv import load_dotenv
    code_root = Path(__file__).resolve().parents[2]
    source_data = Path(os.environ.get('EMI_DATA_DIR') or code_root)
    env_file = Path(os.environ.get('EMI_ENV_FILE') or source_data / '.env')
    load_dotenv(env_file, override=False)
    os.environ['EMI_ENV_FILE'] = str(env_file)
    os.environ['USE_TEST_DB'] = 'true'
    os.environ['TEST_DATABASE_URI_EMI'] = 'sqlite:///' + runtime_db.as_posix()
    os.environ['EMI_DATA_DIR'] = str(output / 'runtime')
    # The standard test bootstrap wires AgentFactory without launching the app. External
    # tool discovery and resource-template compilation are unnecessary here and could
    # otherwise touch unrelated application files. No model calls are mocked.
    from unittest.mock import patch
    from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
    from app.resource_manager.resource_manager import ResourceManager
    with patch.object(ToolRegistry, 'load_tools'), \
         patch.object(ToolRegistry, 'load_mcp_servers'), \
         patch.object(ToolRegistry, 'load_mcp_tool_cache'), \
         patch.object(ResourceManager, 'load_all_from_directory'):
        import app.assistant.tests.test_setup  # noqa: F401
    from app.models.base import get_session
    from app.models.llm_call_log import LLMCallLog
    session = get_session()
    try:
        LLMCallLog.__table__.create(session.get_bind(), checkfirst=True)
    finally:
        session.close()
    from belief_engine.review.experiment import (
        ReviewCopy, make_working_copy, investigate, commit_review, standard_invoke,
    )
    if not args.working.exists():
        make_working_copy(args.baseline, args.working)
    store = ReviewCopy(args.baseline, args.working)
    catalog = {b['belief_key']: b['id'] for b in store.catalog()}
    insights = json.loads(args.insights.read_text(encoding='utf-8')) if args.insights else []
    for case in json.loads(args.cases.read_text(encoding='utf-8')):
        name = case['name']
        if not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in name):
            raise ValueError('Case names must be lowercase letters, digits, underscores or hyphens')
        folder = output / name
        folder.mkdir(exist_ok=False)  # Preserve previous attempts; never overwrite transcripts.
        call_number = 0

        def invoke(agent_name, payload):
            nonlocal call_number
            call_number += 1
            (folder / f'call_{call_number}_input.json').write_text(
                json.dumps({'agent': agent_name, 'input': payload}, indent=2, ensure_ascii=False), encoding='utf-8')
            result = standard_invoke(agent_name, payload)
            (folder / f'call_{call_number}_output.json').write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
            return result

        decision, context, _ = investigate(
            store, invoke, question=case['question'], focal_ids=[catalog[k] for k in case['belief_keys']], insights=insights)
        # Save the proposed result before attempting persistence, including on validation failure.
        (folder / 'proposal.json').write_text(json.dumps(decision, indent=2, ensure_ascii=False), encoding='utf-8')
        decision, context, receipt = commit_review(
            store, invoke, run_id=name, decision=decision, context=context)
        for label, value in (('decision', decision), ('context', context), ('receipt', receipt)):
            (folder / f'{label}.json').write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps(receipt))


if __name__ == '__main__':
    main()
