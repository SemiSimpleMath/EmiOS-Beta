from datetime import datetime, timezone
import json
import threading
import uuid
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message, UserMessage, UserMessageData
from app.assistant.agent_classes.Agent import Agent

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class EmiResultHandler(Agent):
    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)
        self.blackboard = Blackboard()
        # Serializes event-driven activations: this handler owns ONE private
        # blackboard and resets it per result, so two managers finishing at
        # the same moment would otherwise interleave reset/prompt-build and
        # cross-contaminate. (_set_agent_busy is a status flag, not a lock.)
        self._handle_lock = threading.Lock()

    def emi_result_request_handler(self, message: Message):
        logger.info(f"[{self.name}] Handling external EMI result message")
        self.action_handler(message)

    def action_handler(self, message: Message):
        with self._handle_lock:
            return self._handle_result(message)

    def _handle_result(self, message: Message):
        self._set_agent_busy()
        try:
            self._update_blackboard_state(message)

            tool_result = message.tool_result
            if tool_result is None:
                logger.error("[%s] Missing tool_result in emi_result_request message.", self.name)
                raise ValueError(f"[{self.name}] tool_result is required.")

            content = tool_result.content
            self.blackboard.reset_blackboard()
            
            # Populate local blackboard with result data
            try:
                data = json.loads(content) if isinstance(content, str) else None
                if not isinstance(data, dict):
                    raise TypeError(f"Parsed tool result is not a dict: {type(data)}")
                for key, value in data.items():
                    self.blackboard.update_state_value(key, value)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"[{self.name}] Content is not JSON, using as-is: {e}")
                logger.debug("[%s] tool_result content parse exception details", self.name, exc_info=True)
                # Content might be a plain string or structured object.
                self.blackboard.update_state_value("content", "" if content is None else str(content))

            try:
                messages = self.construct_prompt(message)
            except Exception as e:
                logger.error(f"[{self.name}] Error during prompt construction: {e}")
                logger.debug("[%s] prompt construction exception details", self.name, exc_info=True)
                raise

            schema = self.config.get('structured_output')
            result = self._run_llm_with_schema(messages, schema)

            # Add the original user message to the global blackboard for history
            if isinstance(message.agent_input, str) and message.agent_input.strip():
                user_chat_msg = Message(
                    data_type="user_msg",
                    content=message.agent_input,
                    is_chat=True,
                    role="user",
                )
                DI.global_blackboard.add_msg(user_chat_msg)

            try:
                result = self.process_llm_result(result)
            except Exception as e:
                logger.error(f"[{self.name}] Error processing LLM result: {e}")
                logger.debug("[%s] process_llm_result exception details", self.name, exc_info=True)
                raise

            return result
        except Exception as e:
            logger.error(f"[{self.name}] Unhandled exception in action_handler: {e}")
            logger.debug("[%s] action_handler exception details", self.name, exc_info=True)
            raise
        finally:
            # ALWAYS release the busy lock, even on exceptions
            try:
                self._set_agent_idle()
            except Exception as e:
                logger.error(f"[{self.name}] Failed to release busy lock: {e}")
                logger.debug("[%s] busy lock release exception details", self.name, exc_info=True)



    def get_user_prompt(self, message: Message = None):
        return super().get_user_prompt(message)


    def process_llm_result(self, response):
        if not isinstance(response, dict):
            logger.error("[%s] Expected dict response from LLM, got: %r", self.name, type(response))
            raise TypeError(f"[{self.name}] Result handler expects dict response.")

        def _stringify(value):
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            if isinstance(value, (dict, list)):
                try:
                    return json.dumps(value, ensure_ascii=False)
                except Exception as e:
                    logger.error("[%s] Failed JSON serialization in result stringify: %s", self.name, e)
                    logger.debug("[%s] stringify serialization exception details", self.name, exc_info=True)
            return str(value)

        task = _stringify(response.get("task"))
        answer = _stringify(response.get("answer"))
        interesting_info = _stringify(response.get("interesting_info"))
        methods = _stringify(response.get("methods"))
        sources = _stringify(response.get("sources"))
        meta_data = _stringify(response.get("meta_data"))
        feed_str = _stringify(response.get("feed"))

        chat_parts = [task, answer, interesting_info, methods, sources, meta_data]
        chat_str = "\n".join([part for part in chat_parts if part and part.strip()]).strip()
        if not chat_str:
            logger.error("[%s] Empty chat result composed from response: %r", self.name, response)
            raise ValueError(f"[{self.name}] Result handler produced empty chat string.")

        content = {"chat": chat_str, "feed": feed_str }


        id_str = str(uuid.uuid4())
        user_msg_bb = Message(
            data_type='emi_msg',
            sender=self.name,
            receiver=None,
            content=json.dumps(content),
            timestamp= datetime.now(timezone.utc),
            id=id_str,
            role='assistant',
            is_chat=True,
        )
        user_msg_data = UserMessageData(chat=chat_str, feed=feed_str)  # i guess usermessagedata needs strings?
        user_msg_chat = UserMessage(
            data_type='user_msg',
            sender=self.name,
            receiver=None,
            timestamp= datetime.now(timezone.utc),
            id=id_str,
            role='assistant',
            user_message_data=user_msg_data
        )

        global_blackboard = DI.global_blackboard

        global_blackboard.add_msg(user_msg_bb)
        self.components.chat_publisher.publish_chat_to_user(agent=self, message=user_msg_chat)

        return
