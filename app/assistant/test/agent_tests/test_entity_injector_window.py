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


def test_entity_scan_blocks_recent_history_limited_to_last_five_messages():
    history = "\n\n".join([f"[12:0{i}] User: message {i}" for i in range(7)])
    blocks = EntityInjector()._collect_scan_blocks(
        user_context={"recent_history": history},
        scan_keys=["recent_history"],
    )
    expected = "\n\n".join([f"[12:0{i}] User: message {i}" for i in range(2, 7)])
    assert blocks["recent_history"] == expected


def test_entity_injector_filters_assistant_entity_name():
    injector = EntityInjector()
    entities = ["Jukka", "Emi", "assistant", "emi agent", "Katy"]
    filtered = injector._filter_entities_for_prompt(entities)
    assert "Emi" not in filtered
    assert "assistant" not in filtered
    assert "emi agent" not in filtered
    assert "Jukka" in filtered
    assert "Katy" in filtered


def test_entity_field_expands_for_contact_keywords():
    injector = EntityInjector()
    fields = injector._expand_entity_field_keys_for_keywords(
        ["summary"],
        "what is my brother phone number?",
    )
    assert "summary" in fields
    assert "metadata" in fields
    assert "key_facts" in fields


def test_entity_field_expands_for_profile_keywords():
    injector = EntityInjector()
    fields = injector._expand_entity_field_keys_for_keywords(
        ["summary"],
        "tell me about my brother",
    )
    assert "summary" in fields
    assert "level_0" in fields


def test_entity_detection_ranks_by_scan_text_order():
    ranked = EntityInjector()._rank_entities(
        ["Katy", "Mark", "Katy"],
        "Mark called first. Katy was mentioned later. Katy again.",
    )
    assert ranked[0] == "Mark"
    assert "Katy" in ranked


def test_entity_scan_keys_default_when_not_configured():
    class _Blackboard:
        @staticmethod
        def get_state_value(_key, default=None):
            return default

    class _Agent:
        name = "emi_agent"
        config = {}
        blackboard = _Blackboard()

    scan_keys = EntityInjector()._resolve_scan_keys(_Agent())
    assert scan_keys == ["incoming_message", "task", "information", "recent_history"]
