import traceback

from app.models.base import get_session
from app.assistant.maintenance_manager.save_to_unified_db import save_to_unified_db
from app.assistant.utils.logging_config import get_maintenance_logger

logger = get_maintenance_logger(__name__)


def save_chat_messages(blackboard, db_session=None, force_test_db=False):
    """
    Persist chat stream messages through centralized ingestion policy.
    """
    own_session = False
    if db_session is None:
        db_session = get_session(force_test_db=force_test_db)
        own_session = True

    try:
        chat_messages = [msg for msg in blackboard.get_messages() if bool(getattr(msg, "is_chat", False))]

        if not chat_messages:
            logger.debug("No chat messages to save.")
            return

        save_to_unified_db(chat_messages, source="chat", db_session=db_session)
        logger.debug("Chat history saved via unified log writer.")

    except Exception as e:
        db_session.rollback()
        logger.error("Error saving chat history: %s", e)
        logger.error(traceback.format_exc())
        raise

    finally:
        if own_session:
            db_session.close()
