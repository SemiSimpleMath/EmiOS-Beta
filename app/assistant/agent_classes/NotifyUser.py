# Note to coding agents: This file should not be modified without user permission.
from datetime import datetime, timezone
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message, UserMessage, UserMessageData
from app.assistant.agent_classes.Agent import Agent
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

class NotifyUser(Agent):
    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)

    def notify_user_request_handler(self, message: Message):
        logger.info(f"[{self.name}] Handling notify_user message")
        self.action_handler(message)


    def action_handler(self, message: Message):
        self._set_agent_busy()
        try:
            self.blackboard = Blackboard() # make sure we use local blackboard
            logger.debug("[%s] notify_user message: %s", self.name, message)
            tool_result = message.tool_result
            logger.debug("[%s] tool_result: %s", self.name, tool_result)
            notify_info = tool_result.content
            logger.debug("[%s] notify_info: %s", self.name, notify_info)

            self.blackboard.update_state_value('notify_info', notify_info)

            try:
                messages = self.construct_prompt(message)
            except Exception as e:
                logger.error(f"[{self.name}] Error during prompt construction: {e}")
                raise

            schema = self.config.get('structured_output')
            result = self._run_llm_with_schema(messages, schema)

            # Add the original user message to the global blackboard for history
            user_chat_msg = Message(
                data_type="user_msg", 
                content=message.agent_input, 
                is_chat=True,
                role='user'  # Set the role for user messages
            )
            DI.global_blackboard.add_msg(user_chat_msg)

            try:
                result = self.process_llm_result(result)
            except Exception as e:
                logger.error(f"[{self.name}] Error processing LLM result: {e}")
                raise

            return result
        except Exception as e:
            logger.error(f"[{self.name}] Unhandled exception in action_handler: {e}")
            raise
        finally:
            # ALWAYS release the busy lock, even on exceptions
            try:
                self._set_agent_idle()
            except Exception as e:
                logger.error(f"[{self.name}] Failed to release busy lock: {e}")

    def process_llm_result(self, response):
        logger.debug("[%s] LLM response: %s", self.name, response)
        tts_str = response.get('tts_str')
        formatted_str = response.get('formatted_str')


        user_msg_data = UserMessageData(
            feed=formatted_str,
            tts=True,
            tts_text=tts_str
        )

        user_msg_feed = UserMessage(
            data_type='user_msg',
            sender=self.name,
            receiver=None,
            timestamp= datetime.now(timezone.utc),
            role='assistant',
            user_message_data=user_msg_data
        )

        notify_msg = UserMessage(
            data_type='user_msg',
            sender=self.name,
            receiver=None,
            timestamp= datetime.now(timezone.utc),
            role='assistant',
            user_message_data=UserMessageData(
                widget_data=[{
                    "data_type": "notify_user",
                    "message": formatted_str,
                }]
            )
        )

        self.publish_to_user(user_msg_feed)
        self.publish_to_user(notify_msg)

        return

    def publish_to_user(self, message: Message):
        message.event_topic = 'socket_emit'
        try:
            if self._active_request_id and not getattr(message, "request_id", None):
                message.request_id = self._active_request_id
            if self._active_reply_to:
                if message.metadata is None or not isinstance(message.metadata, dict):
                    message.metadata = {}
                message.metadata.setdefault("reply_to", self._active_reply_to)
        except Exception as e:
            logger.debug("Failed to decorate message with request_id/reply_to: %s", e, exc_info=True)
        DI.event_hub.publish(message)