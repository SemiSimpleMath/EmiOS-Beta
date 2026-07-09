# Note to coding agents: This file should not be modified without user permission.
from datetime import datetime, timezone
import threading
import uuid

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.utils.pydantic_classes import Message, UserMessage, UserMessageData
from app.assistant.agent_classes.Agent import Agent


from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class EmiReminderHandler(Agent):
    def __init__(self, name, blackboard, agent_registry, tool_registry, llm_params=None, parent=None):
        super().__init__(name, blackboard, agent_registry, tool_registry, llm_params, parent)
        # Reminder handler should keep scheduler payload shaping isolated from global chat state.
        self.blackboard = Blackboard()
        # Serializes scheduler-fired activations over the ONE private
        # blackboard this handler resets per reminder (_set_agent_busy is a
        # status flag, not a lock).
        self._handle_lock = threading.Lock()

    def scheduler_event_interval_handler(self, message):
        #print("At emi scheduler event handler" , message)
        self.action_handler(message)

    def scheduler_event_one_time_event_handler(self, message):
        #print("At emi scheduler event handler" , message)
        self.action_handler(message)

    def action_handler(self, message: Message):
        # Check if scheduler/reminders feature is enabled
        from app.assistant.user_settings_manager.user_settings import can_run_feature, get_settings_manager
        if not can_run_feature('scheduler'):
            logger.info("⏸️ Scheduler feature disabled in settings - skipping reminder")
            return None

        # Check if quiet mode is active for scheduler/reminders using user's configured quiet hours
        from zoneinfo import ZoneInfo
        settings_manager = get_settings_manager()
        if settings_manager.is_quiet_mode_active('scheduler'):
            now_local = datetime.now(ZoneInfo("America/Los_Angeles"))
            logger.info(f"⏸️ Quiet hours active for scheduler - skipping reminder (current time: {now_local.strftime('%H:%M %Z')})")
            return None

        with self._handle_lock:
            return self._handle_reminder(message)

    def _handle_reminder(self, message: Message):
        self._set_agent_busy()
        try:
            self._update_blackboard_state(message)

            message_data = message.data if isinstance(message.data, dict) else {}
            event_payload = message_data.get("event_payload") if isinstance(message_data, dict) else None
            if not isinstance(event_payload, dict):
                logger.error("[%s] Missing or invalid event_payload in scheduler message: %r", self.name, message.data)
                raise ValueError(f"[{self.name}] scheduler message requires data.event_payload dict.")

            payload_content = event_payload.get("payload_message")
            if not isinstance(payload_content, str) or not payload_content.strip():
                logger.error("[%s] Missing or invalid payload_message in scheduler event: %r", self.name, event_payload)
                raise ValueError(f"[{self.name}] scheduler event requires non-empty payload_message.")

            reminder_dict = {"reminder_data": payload_content}

            self.blackboard.reset_blackboard()
            # Populate local blackboard with reminder data
            for key, value in reminder_dict.items():
                self.blackboard.update_state_value(key, value)

            # resolve_resource requires scope_context on the blackboard.
            # This handler is scheduler-fired (runs outside any manager, no
            # scope-bearing caller), so it self-scopes from its own source.
            try:
                from app.assistant.scope.loader import load_scope_for_source
                _scope = load_scope_for_source(
                    kind="subsystem",
                    source_id="system_reminder",
                    actor_id="emi_reminder_handler",
                    identity_overrides={"owner_id": "system", "surface": "system"},
                )
                self.blackboard.update_state_value("scope_context", _scope.model_dump())
            except Exception as e:
                logger.error("[%s] Failed building scope_context for reminder handler: %s", self.name, e)
                logger.debug("[%s] scope_context build exception details", self.name, exc_info=True)
                raise

            try:
                messages = self.construct_prompt(message)
            except Exception as e:
                logger.error(f"[{self.name}] Error during prompt construction: {e}")
                logger.debug("[%s] prompt construction exception details", self.name, exc_info=True)
                raise

            schema = self.config.get('structured_output')

            result = self._run_llm_with_schema(messages, schema)

            result = self.process_llm_result(result)

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
        """
        Use parent class's get_user_prompt() which includes entity detection.
        """
        # Parent's get_user_prompt already handles:
        # - Loading user prompt template
        # - Generating context injections
        # - Entity detection and injection
        return super().get_user_prompt(message)

    def _persist_reminder_durably(self, reminder_text: str) -> None:
        """Give the reminder the durable record its message class deserves:
        always a unified_log row; plus a cron_reminder ticket when no
        master_room client is live to receive the socket emission."""
        try:
            from app.assistant.message_manager.save_to_unified_db import save_to_unified_db
            save_to_unified_db([{
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now(timezone.utc),
                "role": "assistant",
                "message": f"⏰ Reminder: {reminder_text}",
                "room_id": "master_room",
                "room_surface": "ui",
                "room_context_id": "main",
                "direction": "outbound",
                "metadata_json": {"reminder": True},
                "sub_data_type": ["scheduler_reminder"],
            }], source="scheduler_reminder")
        except Exception:
            logger.error("[%s] failed persisting reminder to unified_log", self.name, exc_info=True)

        try:
            from app.services.socket_manager import RoomNotBound
            from app.assistant.ServiceLocator.service_locator import DI as _DI
            _DI.socket_manager.resolve_socket("master_room")
        except RoomNotBound:
            from app.assistant.ticket_manager.ticket_service import propose_notice_ticket
            logger.warning(
                "[%s] no live master_room client — reminder rides the ticket channel",
                self.name,
            )
            propose_notice_ticket(
                title="Reminder",
                message=reminder_text,
                suggestion_type="scheduler_reminder",
                trigger_reason="reminder fired with no live UI client",
                ticket_type="cron_reminder",
            )
        except Exception:
            logger.error("[%s] socket liveness check failed", self.name, exc_info=True)


    def process_llm_result(self, response):
        if not isinstance(response, dict):
            logger.error("[%s] Expected dict response from LLM, got: %r", self.name, type(response))
            raise TypeError(f"[{self.name}] Reminder handler expects dict response.")

        tts_raw = response.get("tts_str")
        if not isinstance(tts_raw, str) or not tts_raw.strip():
            logger.error("[%s] Missing/invalid tts_str in reminder response: %r", self.name, tts_raw)
            raise ValueError(f"[{self.name}] tts_str must be a non-empty string.")

        feed_raw = response.get("feed_str")
        feed_str = feed_raw if isinstance(feed_raw, str) else ""
        tts_str = tts_raw.strip()
        content = f"Reminder came from scheduler: {tts_str}"

        # Durability (2026-07-08 delivery audit D3): a fired reminder used to
        # exist ONLY as a socket emission + a write to this handler's private
        # in-memory blackboard — browser closed at fire time meant the
        # reminder was permanently lost. Now the reminder always persists to
        # unified_log, and when no master_room client is live it ALSO rides
        # the durable ticket channel (DB + popup/poll), matching the delivery
        # guarantees tickets already have. Both are best-effort wrappers —
        # a persistence hiccup must not stop the live delivery attempt.
        self._persist_reminder_durably(tts_str)

        id_str = str(uuid.uuid4())
        user_msg_bb = Message(
            data_type='emi_msg',
            sender=self.name,
            receiver=None,
            content=content,
            timestamp= datetime.now(timezone.utc),
            id=id_str,
            role='assistant',
            is_chat=True,
        )
        user_msg_data = UserMessageData(
            feed=feed_str,
            tts=True,
            tts_text=tts_str
        )

        user_msg_feed = UserMessage(
            data_type='user_msg',
            sender=self.name,
            receiver=None,
            timestamp= datetime.now(timezone.utc),
            id=id_str,
            role='assistant',
            user_message_data=user_msg_data
        )


        self.blackboard.add_msg(user_msg_bb)
        self.components.chat_publisher.publish_chat_to_user(agent=self, message=user_msg_feed)

        return


