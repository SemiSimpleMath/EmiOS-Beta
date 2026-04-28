"""
context_engine — Context formation system for Emi.

Stages:
    chat_scan          → detects activation-worthy signals in recent chat
    context_activation → multi_seed_activation → situation_brief (KG + LLM)
    reasoning_agent    → single emi_team_manager loop: research + surface questions

Entry point:
    from app.assistant.context_engine.pipeline import run_context_activation
"""
