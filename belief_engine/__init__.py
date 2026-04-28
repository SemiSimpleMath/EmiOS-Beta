"""
Belief Engine — isolated inference system for user beliefs.

Completely separate from the existing memory system.
Run alongside it to evaluate quality before integration.

Quick start:
    # 1. Ensure schema (once)
    python app/assistant/test/tmp_run_belief_engine.py --migrate

    # 2. Run inference for routine domain
    python app/assistant/test/tmp_run_belief_engine.py --domain routine

    # 3. Inspect output
    python app/assistant/test/tmp_run_belief_engine.py --list
    cat resources/kg_derived/resource_user_beliefs.json
"""
