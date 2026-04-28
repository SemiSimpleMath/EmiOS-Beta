import os
from typing import Any, Dict, List, Optional

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ChatRequestNormalizer:
    def get_first_image_attachment(self, message) -> Optional[Dict[str, Any]]:
        if not message:
            return None
        meta = getattr(message, "metadata", None)
        if not isinstance(meta, dict):
            return None
        attachments = meta.get("attachments")
        if not isinstance(attachments, list):
            return None
        for att in attachments:
            if isinstance(att, dict) and att.get("type") == "image" and att.get("path"):
                return att
        return None

    def parse_slash_command(self, text: str) -> Optional[Dict[str, str]]:
        if not isinstance(text, str):
            return None
        raw = text
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        body = stripped[1:]
        if not body:
            return None
        parts = body.split(None, 1)
        name = (parts[0] or "").strip().lower()
        payload = (parts[1] if len(parts) > 1 else "").strip()
        if not name:
            return None
        return {"name": name, "payload": payload, "raw": raw}

    def normalize_interaction_metadata(self, message) -> Dict[str, Any]:
        original = getattr(message, "metadata", None)
        meta = dict(original) if isinstance(original, dict) else {}
        requested_speaking = bool(meta.get("tts_requested", False))

        if isinstance(meta.get("interaction_mode"), str) and meta.get("interaction_mode").strip():
            interaction_mode = meta.get("interaction_mode").strip().lower()
        else:
            interaction_mode = "speaking" if requested_speaking else "text"

        meta["interaction_mode"] = interaction_mode
        meta["tts_requested"] = bool(meta.get("tts_requested", interaction_mode == "speaking"))
        return meta

    def is_speaking_mode(self, message) -> bool:
        meta = self.normalize_interaction_metadata(message)
        return bool(meta.get("tts_requested", False))

    @staticmethod
    def is_room_scoped_message(msg) -> bool:
        try:
            rid = getattr(msg, "room_id", None)
            if isinstance(rid, str) and rid.strip():
                return True
            surface = getattr(msg, "room_surface", None)
            if isinstance(surface, str) and surface.strip():
                return True
            data = getattr(msg, "data", None)
            if isinstance(data, dict):
                rid2 = data.get("room_id")
                if isinstance(rid2, str) and rid2.strip():
                    return True
                surface2 = data.get("room_surface")
                if isinstance(surface2, str) and surface2.strip():
                    return True
            meta = getattr(msg, "metadata", None)
            if isinstance(meta, dict):
                reply_to = meta.get("reply_to")
                if isinstance(reply_to, dict):
                    rt = str(reply_to.get("type") or "").strip().lower()
                    if rt in {"slack", "twilio_sms", "telegram"}:
                        return True
        except Exception as e:
            logger.error("Failed room-scope detection: %s", e)
            logger.debug("room-scope detection exception details", exc_info=True)
            return False
        return False

    def build_user_history_payload(self, message) -> Dict[str, Any]:
        content = getattr(message, "agent_input", None)
        metadata = None
        sub_types: List[str] = []

        cmd = None
        try:
            cmd = self.parse_slash_command(content) if content else None
        except Exception as e:
            logger.error("Failed slash command parse: %s", e)
            logger.debug("slash command parse exception details", exc_info=True)

        if cmd and cmd.get("name"):
            sub_types = ["slash_command", cmd.get("name")]
            scope = cmd.get("name")
            metadata = {
                "command_name": cmd.get("name"),
                "command_scope": scope,
                "command_payload": cmd.get("payload") or "",
                "raw_content": cmd.get("raw") or "",
            }
            content = cmd.get("payload") or ""
        else:
            att = self.get_first_image_attachment(message)
            if att:
                fname = att.get("original_filename") or os.path.basename(str(att.get("path") or "image"))
                marker = f"[image attached: {fname}]"
                if isinstance(content, str) and content.strip():
                    content = f"{content}\n{marker}"
                else:
                    content = marker

        if not isinstance(content, str):
            content = "" if content is None else str(content)

        return {
            "content": content,
            "metadata": metadata,
            "sub_data_type": sub_types,
            "slash_command": cmd,
        }

    def apply_image_attachment_to_prompt(self, *, agent_name: str, messages: List[Dict[str, Any]], message) -> List[Dict[str, Any]]:
        att = self.get_first_image_attachment(message)
        if not att:
            return messages

        try:
            image_path = str(att.get("path"))
            fname = att.get("original_filename") or os.path.basename(image_path)
            marker = f"\n\n[image attached: {fname}]"
            user_text = messages[1].get("content", "")
            if not isinstance(user_text, str):
                user_text = str(user_text)
            messages[1]["content"] = [
                {"type": "input_text", "text": f"{user_text}{marker}"},
                {"type": "image_path", "path": image_path},
            ]
        except Exception as e:
            logger.error("[%s] Failed to build multimodal prompt blocks: %s", agent_name, e)
            logger.debug("[%s] multimodal prompt block exception details", agent_name, exc_info=True)
        return messages

