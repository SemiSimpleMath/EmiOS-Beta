import uuid
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import ToolResult
from app.models.base import get_session
from app.assistant.news.news_operations import record_news_feedback
from app.assistant.ServiceLocator.service_locator import DI

logger = get_logger(__name__)

class PreferenceManager:
    def __init__(self):
        pass

    def handle_feedback(self, feedback_msg):
        """
        Handle thumbs up/down feedback by calling LabelAgent directly,
        then update user preferences in the DB.
        """
        try:
            feedback_msg.request_id = str(uuid.uuid4())
            label_agent = DI.agent_factory.create_agent('label')
            result = label_agent.action_handler(feedback_msg)
            if result:
                self._handle_label_response(result, feedback_msg)
            else:
                logger.warning("No result returned from label agent.")
        except Exception as e:
            logger.error(f"Error in handle_feedback: {e}")

    def _handle_label_response(self, tool_result: ToolResult, original_message):
        """
        Process labeled results and update feedback in DB.
        """
        try:
            if not tool_result or not tool_result.data_list:
                logger.warning("Empty or invalid tool_result.")
                return

            session = get_session()
            try:
                for item in tool_result.data_list:
                    category = item.get("category")
                    labels = item.get("labels", [])
                    
                    # Get preference from the original message data
                    preference = original_message.data.get("preference")
                    feedback_value = 5 if preference == "like" else -5 if preference == "dislike" else None
                    if feedback_value is None:
                        logger.warning(f"Unknown preference: {preference}")
                        continue

                    article_title = original_message.data.get("title")
                    article_url = original_message.data.get("id")  # Treat ID as URL

                    if not category or not labels or not article_title or not article_url:
                        logger.warning(f"Missing required data: category={category}, labels={labels}, title={article_title}, url={article_url}")
                        continue

                    record_news_feedback(
                        session=session,
                        article_title=article_title,
                        article_url=article_url,
                        category_name=category,
                        labels=labels,
                        feedback_value=feedback_value,
                        commit=False
                    )
                session.commit()
                logger.info("Successfully updated preference scores.")
            finally:
                session.close()

        except Exception as e:
            logger.error(f"Error in _handle_label_response: {e}")

