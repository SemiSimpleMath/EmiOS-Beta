import uuid
from datetime import datetime, timezone
from typing import Tuple

from app.assistant.utils.pydantic_classes import Message, UserMessage, UserMessageData


class ChatResponseBuilder:
    def build_user_response_messages(
        self,
        *,
        sender: str,
        chat_text: str,
        tts_requested: bool,
    ) -> Tuple[Message, UserMessage]:
        msg_id = str(uuid.uuid4())
        user_msg_bb = Message(
            data_type="emi_msg",
            sender=sender,
            receiver=None,
            content=chat_text,
            timestamp=datetime.now(timezone.utc),
            id=msg_id,
            role="assistant",
            is_chat=True,
        )

        user_msg_data = UserMessageData(
            chat=chat_text,
            tts=bool(tts_requested),
            tts_text=chat_text if bool(tts_requested) else None,
        )
        user_msg_chat = UserMessage(
            data_type="user_msg",
            sender=sender,
            receiver=None,
            timestamp=datetime.now(timezone.utc),
            id=msg_id,
            role="assistant",
            user_message_data=user_msg_data,
        )
        return user_msg_bb, user_msg_chat

    def build_team_delegation_messages(
        self,
        *,
        sender: str,
        msg_for_agent: str,
        information_for_agent: str,
    ) -> Tuple[Message, Message]:
        agent_msg = Message(
            task=msg_for_agent,
            data_type="agent_msg",
            sender=sender,
            receiver=None,
            content=msg_for_agent,
            information=information_for_agent,
            role="assistant",
        )
        task_notification_msg = Message(
            task=msg_for_agent,
            data_type="agent_msg",
            sub_data_type=["agent_notification"],
            sender=sender,
            receiver=None,
            content=(
                f"Following message was sent to the team: [{msg_for_agent}] \n"
                "This is now in progress and no further action is necessary until we hear from the team. "
                "REPEAT Do not take action again unless specifically told to do so!\n"
            ),
            information=information_for_agent,
            role="user",
            is_chat=True,
        )
        return agent_msg, task_notification_msg

