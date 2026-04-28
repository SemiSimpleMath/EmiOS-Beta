from app.assistant.log_ingestion import LogIngestionPolicy


def test_chat_policy_allows_normal_message():
    allowed, reason = LogIngestionPolicy.should_persist_payload(
        source="chat",
        payload={"message": "hello world", "sub_data_type": []},
    )
    assert allowed is True
    assert reason == "chat_allowed"


def test_chat_policy_blocks_room_scoped_payload():
    allowed, reason = LogIngestionPolicy.should_persist_payload(
        source="chat",
        payload={"message": "room message", "room_id": "justin"},
    )
    assert allowed is False
    assert reason == "room_scoped"


def test_chat_policy_blocks_excluded_sub_data_type():
    allowed, reason = LogIngestionPolicy.should_persist_payload(
        source="chat",
        payload={"message": "summary", "sub_data_type": ["history_summary"]},
    )
    assert allowed is False
    assert reason == "excluded_sub_data_type"


def test_chat_policy_blocks_test_mode():
    allowed, reason = LogIngestionPolicy.should_persist_payload(
        source="chat",
        payload={"message": "debug-only", "test_mode": True},
    )
    assert allowed is False
    assert reason == "test_mode"


def test_room_source_allows_room_payload():
    allowed, reason = LogIngestionPolicy.should_persist_payload(
        source="room_slack",
        payload={"message": "hello from slack room", "room_id": "team"},
    )
    assert allowed is True
    assert reason == "room_allowed"
