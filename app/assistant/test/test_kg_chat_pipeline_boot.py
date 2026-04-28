import os
import sys
from pathlib import Path

# Hard safety: force this test to use a dedicated SQLite test DB.
# Must be set before test_setup initializes DI/services.
os.environ["USE_TEST_DB"] = "true"
os.environ.setdefault("TEST_DB_NAME", "test_kg_chat_pipeline")

# Ensure repo root is on sys.path so `import app...` works when running by file path.
_HERE = Path(__file__).resolve()
for parent in _HERE.parents:
    if (parent / "app").is_dir():
        sys.path.insert(0, str(parent))
        break

# Ensure required baseline tables exist in the dedicated test DB
# before test_setup initializes services that query them.
from app.assistant.database.db_handler import initialize_database as _init_core_db
from app.assistant.entity_management.entity_cards import initialize_entity_cards_db as _init_entity_cards_db

_init_core_db(force_test_db=True)
_init_entity_cards_db(force_test_db=True)

# Full non-UI bootstrap for DI/services used in tests.
import app.assistant.tests.test_setup as test_imports  # noqa: F401

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.pipelines.pipeline_registry import resolve_pipeline
from app.models.base import get_database_uri


def _assert_test_db_safety() -> None:
    uri = get_database_uri()
    lower = (uri or "").lower()
    # test_setup currently forces TEST_DB_NAME=test_emidb in some paths.
    is_test_uri = ("test_kg_chat_pipeline" in lower) or ("test_emidb" in lower) or ("/test_" in lower)
    if not is_test_uri:
        raise RuntimeError(
            "Refusing to run KG pipeline test against non-test database. "
            f"Resolved DB URI: {uri}"
        )


def test_kg_chat_pipeline_bootstrap_and_db_only_steps():
    _assert_test_db_safety()
    pipeline = resolve_pipeline("kg_chat_pipeline")
    assert pipeline is not None, "kg_chat_pipeline failed to resolve"
    assert DI.agent_factory is not None, "agent_factory missing from DI"
    assert DI.tool_registry is not None, "tool_registry missing from DI"

    result = pipeline.run(
        only_steps=[
            "project_chat_from_unified_log",
            "build_conversation_windows",
        ],
        force=True,
    )
    assert result.get("status") == "success"
    names = [s.get("name") for s in result.get("steps", [])]
    assert "project_chat_from_unified_log" in names
    assert "build_conversation_windows" in names


def test_kg_chat_pipeline_parser_step_live_if_api_key():
    _assert_test_db_safety()
    # Require explicit opt-in to avoid accidental long-running/costly live calls.
    if os.environ.get("KG_CHAT_PIPELINE_LIVE") != "1":
        return
    if not os.environ.get("OPENAI_API_KEY"):
        # Keep this test deterministic in CI/dev without live credentials.
        return

    pipeline = resolve_pipeline("kg_chat_pipeline")
    assert pipeline is not None
    assert DI.agent_factory is not None

    result = pipeline.run(
        only_steps=["parse_conversation_windows"],
        force=True,
    )
    # parse step may be success or skipped depending on queued windows
    assert result.get("status") in {"success"}


def main() -> int:
    test_kg_chat_pipeline_bootstrap_and_db_only_steps()
    test_kg_chat_pipeline_parser_step_live_if_api_key()
    print("✅ kg_chat_pipeline bootstrap test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

