from app.assistant.agent_runtime.services.entity_injector import EntityInjector


def test_entity_scan_blocks_collect_from_configured_context_keys():
    blocks = EntityInjector()._collect_scan_blocks(
        user_context={
            "task": "Find details about Mark.",
            "recent_history": "Earlier: Katy mentioned Mark.\n\nThen Jamie was mentioned.",
            "ignored": "Should not be scanned.",
        },
        scan_keys=["task", "recent_history"],
    )
    assert blocks["task"] == "Find details about Mark."
    assert blocks["recent_history"] == "Earlier: Katy mentioned Mark.\n\nThen Jamie was mentioned."
    assert "ignored" not in blocks


def test_entity_scan_blocks_recent_history_limited_to_last_three_messages():
    # Entity detection over full history is noisy — only the latest 3
    # message blocks are scanned (_limit_recent_history_scan guardrail).
    history = "\n\n".join([f"[12:0{i}] User: message {i}" for i in range(7)])
    blocks = EntityInjector()._collect_scan_blocks(
        user_context={"recent_history": history},
        scan_keys=["recent_history"],
    )
    expected = "\n\n".join([f"[12:0{i}] User: message {i}" for i in range(4, 7)])
    assert blocks["recent_history"] == expected


def test_recent_history_scan_skips_day_marker_blocks():
    history = "CHAT FROM YESTERDAY\n\n[09:00] User: old note\n\nCHAT FROM TODAY\n\n[12:00] User: fresh note"
    limited = EntityInjector._limit_recent_history_scan(history)
    assert "CHAT FROM YESTERDAY" not in limited
    assert "CHAT FROM TODAY" not in limited
    assert "fresh note" in limited


def test_entity_injector_filters_assistant_entity_name():
    injector = EntityInjector()
    entities = ["Jukka", "Emi", "assistant", "emi agent", "Katy"]
    filtered = injector._filter_entities_for_prompt(entities)
    assert "Emi" not in filtered
    assert "assistant" not in filtered
    assert "emi agent" not in filtered
    assert "Jukka" in filtered
    assert "Katy" in filtered


# The keyword-driven field-expansion tests that lived here pinned
# _expand_entity_field_keys_for_keywords — removed with the v1 granular
# field-extraction path; per-key card LEVELS (entity_summary/entity_card
# etc.) are the live semantics.


def test_render_fields_derive_from_declared_entity_keys():
    class _Agent:
        name = "emi_agent"
        config = {}

    fields = EntityInjector._resolve_render_fields(
        agent=_Agent(),
        render_keys=["entity_summary", "entity_metadata", "entity_info"],
    )
    # "info" is excluded by design; the rest strip the entity_ prefix.
    assert fields == ["summary", "metadata"]


def test_entity_detection_ranks_by_scan_text_order():
    ranked = EntityInjector()._rank_entities(
        ["Katy", "Mark", "Katy"],
        "Mark called first. Katy was mentioned later. Katy again.",
    )
    assert ranked[0] == "Mark"
    assert "Katy" in ranked


class _ScopeBlackboard:
    def __init__(self, scope):
        self._scope = scope

    def get_state_value(self, key, default=None):
        if key == "scope_context":
            return self._scope
        return default


def _scoped_agent(entities_policy):
    class _Agent:
        name = "emi_agent"
        config = {}
        blackboard = _ScopeBlackboard({"entities": entities_policy} if entities_policy is not None else None)

    return _Agent()


def test_scope_narrowing_disabled_policy_drops_all_entities():
    agent = _scoped_agent({"enabled": False, "allowed_entity_cards": []})
    out = EntityInjector()._narrow_entities_by_scope(
        agent=agent, user_context={}, entities=["Mark", "Jamie"],
    )
    assert out == []


def test_scope_narrowing_allowlist_filters_case_insensitively():
    agent = _scoped_agent({"enabled": True, "allowed_entity_cards": ["mark"]})
    out = EntityInjector()._narrow_entities_by_scope(
        agent=agent, user_context={}, entities=["Mark", "Jamie"],
    )
    assert out == ["Mark"]


def test_scope_narrowing_empty_allowlist_passes_through():
    agent = _scoped_agent({"enabled": True, "allowed_entity_cards": []})
    out = EntityInjector()._narrow_entities_by_scope(
        agent=agent, user_context={}, entities=["Mark", "Jamie"],
    )
    assert out == ["Mark", "Jamie"]


def test_scope_narrowing_all_marker_passes_through():
    agent = _scoped_agent({"enabled": True, "allowed_entity_cards": ["all"]})
    out = EntityInjector()._narrow_entities_by_scope(
        agent=agent, user_context={}, entities=["Mark", "Jamie"],
    )
    assert out == ["Mark", "Jamie"]


def test_scope_narrowing_without_scope_passes_through():
    agent = _scoped_agent(None)
    out = EntityInjector()._narrow_entities_by_scope(
        agent=agent, user_context={}, entities=["Mark"],
    )
    assert out == ["Mark"]


def test_entity_scan_keys_default_when_not_configured():
    class _Agent:
        name = "emi_agent"
        config = {}

    scan_keys = EntityInjector._resolve_scan_keys(_Agent(), require_explicit=False)
    assert scan_keys == ["incoming_message", "task", "information", "recent_history"]


def test_entity_scan_keys_required_explicit_raises_without_config():
    import pytest

    class _Agent:
        name = "emi_agent"
        config = {}

    with pytest.raises(ValueError, match="entity_scan_keys"):
        EntityInjector._resolve_scan_keys(_Agent(), require_explicit=True)
