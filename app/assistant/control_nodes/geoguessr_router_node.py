from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class GeoguessrRouterNode(ControlNode):
    """
    Deterministic router for game_mode (GeoGuessr).

    Flow each turn:
      1. Reads the host agent's output (chat_response, reveal_answer, game_over).
      2. Updates the session service with any analyst output already merged into blackboard.
      3. Emits a `geo_update` WebSocket event to refresh the panel.
      4. On reveal: emits `geo_reveal` event with the best_guess.
      5. On game_over: deactivates the session and stops the screenshot timer.
      6. Routes to final_answer_node.
    """

    _HOST_AGENT = "master_room::geoguessr_host"

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != self._HOST_AGENT:
            raise ValueError(
                f"[{self.name}] expected last_agent={self._HOST_AGENT!r}, got: {last_agent!r}"
            )

        chat_response = str(self.blackboard.get_state_value("chat_response", "") or "").strip()
        if not chat_response:
            raise ValueError(f"[{self.name}] chat_response must be non-empty.")

        reveal_answer = bool(self.blackboard.get_state_value("reveal_answer", False))
        # best_guess output by the agent when reveal_answer=True
        agent_best_guess = str(self.blackboard.get_state_value("best_guess", "") or "").strip()
        game_over = bool(self.blackboard.get_state_value("game_over", False))

        room_id = str(self.blackboard.get_state_value("room_id", "") or "").strip()
        session_id = str(self.blackboard.get_state_value("geo_session_id", "") or "").strip()

        # Pull current game state from the session service — the local blackboard
        # has no analyst data (the analyst runs in a separate timer thread).
        best_guess = ""
        confidence = 0
        visible_clue = ""
        commentary = ""
        screenshot_count = 0
        latest_screenshot = ""
        if session_id:
            try:
                bb = getattr(DI, "global_blackboard", None)
                if bb is not None:
                    from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
                    session_data = GeoguessrSessionService(blackboard=bb).get_session(session_id=session_id)
                    if session_data:
                        best_guess = str(session_data.get("best_guess") or "")
                        confidence = int(session_data.get("confidence") or 0)
                        clue_log = session_data.get("clue_log") or []
                        visible_clue = clue_log[-1] if clue_log else ""
                        screenshot_count = len(session_data.get("screenshot_paths") or [])
            except Exception:
                logger.debug("[%s] Failed reading session state for geo_update emit", self.name, exc_info=True)
                raise

        # Agent's explicit reveal output takes priority over session's stale best_guess.
        if reveal_answer and agent_best_guess:
            best_guess = agent_best_guess
            logger.debug("[%s] Using agent-output best_guess for reveal: %r", self.name, best_guess)

        # Persist analyst update to session — only for reveal/game_over state changes.
        # Clue/confidence updates are handled by the timer thread directly.

        if reveal_answer and session_id:
            self._mark_revealed(session_id=session_id)
            self._pause_timer(session_id=session_id)
            self.blackboard.update_state_value("geo_answer_revealed", True)

        # Emit panel update
        if room_id:
            self._emit_geo_update(
                room_id=room_id,
                visible_clue=visible_clue,
                commentary=commentary,
                confidence=confidence,
                screenshot_count=screenshot_count,
                latest_screenshot=latest_screenshot,
            )
            if reveal_answer:
                self._emit_geo_reveal(room_id=room_id, best_guess=best_guess, confidence=confidence)
            tts_requested = self._resolve_tts(session_id=session_id)
            if tts_requested:
                self._emit_chat_with_tts(room_id=room_id, text=chat_response)

        if game_over and session_id:
            self._stop_timer_and_deactivate(session_id=session_id)

        cfg = self._cfg()
        final_node = cfg.get("final_node", "final_answer_node")

        self.blackboard.update_state_value(
            "result",
            {
                "final_answer_task": str(self.blackboard.get_state_value("task", "") or ""),
                "final_answer_answer": chat_response,
                "final_answer_what_was_done": "GeoGuessr game mode turn.",
                "final_answer_interesting_info": "",
                "final_answer_sources": [],
            },
        )
        self.blackboard.update_state_value("next_agent", final_node)
        self.blackboard.update_state_value("last_agent", self.name)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _resolve_tts(self, *, session_id: str) -> bool:
        """Check blackboard, reply_to metadata, then session for TTS preference."""
        if bool(self.blackboard.get_state_value("tts_requested", False)):
            return True
        try:
            reply_to = self.blackboard.get_state_value("reply_to", None)
            if isinstance(reply_to, dict) and reply_to.get("tts_requested"):
                return True
        except Exception:
            pass
        if session_id:
            try:
                bb = getattr(DI, "global_blackboard", None)
                if bb is not None:
                    from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
                    session_data = GeoguessrSessionService(blackboard=bb).get_session(session_id=session_id)
                    if session_data and session_data.get("tts_requested"):
                        return True
            except Exception:
                logger.debug("[%s] Failed reading session tts_requested", self.name, exc_info=True)
        return False

    def _update_session(self, *, session_id: str, best_guess: str, confidence: int, clue: str) -> None:
        try:
            bb = getattr(DI, "global_blackboard", None)
            if bb is None:
                return
            from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
            GeoguessrSessionService(blackboard=bb).update_analysis(
                session_id=session_id,
                best_guess=best_guess,
                confidence=confidence,
                clue=clue,
            )
        except Exception:
            logger.debug("[%s] Failed updating geoguessr session %s", self.name, session_id, exc_info=True)
            raise

    def _mark_revealed(self, *, session_id: str) -> None:
        try:
            bb = getattr(DI, "global_blackboard", None)
            if bb is None:
                return
            from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService
            GeoguessrSessionService(blackboard=bb).mark_revealed(session_id=session_id)
        except Exception:
            logger.debug("[%s] Failed marking reveal for session %s", self.name, session_id, exc_info=True)
            raise

    def _pause_timer(self, *, session_id: str) -> None:
        try:
            from app.assistant.game.geoguessr.geo_screenshot_timer import pause_game_timer
            pause_game_timer(session_id=session_id)
        except Exception:
            logger.debug("[%s] Failed pausing geo timer session=%s", self.name, session_id, exc_info=True)
            raise

    def _stop_timer_and_deactivate(self, *, session_id: str) -> None:
        try:
            from app.assistant.game.geoguessr.geo_screenshot_timer import stop_game_timer
            stop_game_timer(session_id=session_id)
        except Exception:
            logger.debug("[%s] Failed stopping geo timer session=%s", self.name, session_id, exc_info=True)
            raise

    def _emit_chat_with_tts(self, *, room_id: str, text: str) -> None:
        try:
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(feed=None, chat=text, tts=True, tts_text=text),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug("[%s] Emitted chat+TTS to room_id %s", self.name, room_id)
        except Exception:
            logger.debug("[%s] Failed emitting chat+TTS", self.name, exc_info=True)
            raise

    def _emit_geo_update(
        self,
        *,
        room_id: str,
        visible_clue: str,
        commentary: str,
        confidence: int,
        screenshot_count: int,
        latest_screenshot: str,
    ) -> None:
        try:
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            payload = {
                "data_type": "geo_update",
                "visible_clue": visible_clue,
                "commentary": commentary,
                "confidence": confidence,
                "screenshot_count": screenshot_count,
                "latest_screenshot": latest_screenshot,
            }
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(widget_data=[payload]),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug("[%s] Emitted geo_update to room_id %s confidence=%d", self.name, room_id, confidence)
        except Exception:
            logger.debug("[%s] Failed emitting geo_update", self.name, exc_info=True)
            raise

    def _emit_geo_reveal(self, *, room_id: str, best_guess: str, confidence: int) -> None:
        try:
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()
            payload = {
                "data_type": "geo_reveal",
                "best_guess": best_guess,
                "confidence": confidence,
            }
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": room_id}},
                user_message_data=UserMessageData(widget_data=[payload]),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug("[%s] Emitted geo_reveal to room_id %s", self.name, room_id)
        except Exception:
            logger.debug("[%s] Failed emitting geo_reveal", self.name, exc_info=True)
            raise

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            return {}
        game_mode = flow_cfg.get("game_mode")
        return game_mode if isinstance(game_mode, dict) else {}
