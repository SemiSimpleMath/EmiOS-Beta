from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.assistant.utils.identity_names import get_required_assistant_name
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root

logger = get_logger(__name__)

SCREENSHOT_INTERVAL_SECONDS = 15

# Active timers keyed by session_id
_active_timers: Dict[str, "_GeoScreenshotTimer"] = {}
_timers_lock = threading.Lock()


class _GeoScreenshotTimer:
    """
    Fires a screenshot + analysis cycle every SCREENSHOT_INTERVAL_SECONDS
    for a single GeoGuessr game session.
    """

    def __init__(
        self,
        *,
        session_id: str,
        socket_id: str,
        room_id: str,
        surface: str,
        context_id: str,
        monitor_index: int = 1,
        tts_requested: bool = False,
    ) -> None:
        self.session_id = session_id
        self.socket_id = socket_id
        self.room_id = room_id
        self.surface = surface
        self.context_id = context_id
        self.monitor_index = monitor_index
        self.tts_requested = tts_requested

        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = paused
        self._thread: threading.Thread | None = None
        self._milestone_announced: set[int] = set()  # confidence thresholds already announced

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._pause_event.set()  # start paused — user must /play resume to begin
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"geo-timer-{self.session_id}",
        )
        self._thread.start()
        logger.info("[geo_timer] Started for session=%s socket=%s", self.session_id, self.socket_id)

    def stop(self) -> None:
        self._stop_event.set()
        self._pause_event.clear()  # unblock if waiting in pause loop
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("[geo_timer] Stopped session=%s", self.session_id)

    def pause(self) -> None:
        self._pause_event.set()
        logger.info("[geo_timer] Paused session=%s", self.session_id)

    def resume(self) -> None:
        self._pause_event.clear()
        logger.info("[geo_timer] Resumed session=%s", self.session_id)

    def reset_round(self) -> None:
        """Clear pause so next tick fires fresh. The session service tracks state."""
        self._pause_event.clear()
        self._milestone_announced.clear()
        logger.info("[geo_timer] Round reset session=%s", self.session_id)

    def _loop(self) -> None:
        # Wait a few seconds before first shot so the user sees the panel open first
        self._stop_event.wait(timeout=5.0)
        if self._stop_event.is_set():
            return

        while not self._stop_event.is_set():
            # If paused, poll every 2s until unpaused or stopped
            if self._pause_event.is_set():
                self._stop_event.wait(timeout=2.0)
                continue
            try:
                self._tick()
            except Exception:
                logger.debug("[geo_timer] tick failed session=%s", self.session_id, exc_info=True)
            self._stop_event.wait(timeout=SCREENSHOT_INTERVAL_SECONDS)

    def _tick(self) -> None:
        """Capture a screenshot, run analyst agent, emit update via socket."""
        from app.assistant.ServiceLocator.service_locator import DI
        from app.assistant.room_session_manager.services.geoguessr_session_service import GeoguessrSessionService

        bb = getattr(DI, "global_blackboard", None)
        if bb is None:
            logger.debug("[geo_timer] No blackboard — skipping tick")
            return

        svc = GeoguessrSessionService(blackboard=bb)
        session = svc.get_session(session_id=self.session_id)
        if not session or session.get("status") != "active":
            logger.info("[geo_timer] Session %s no longer active — stopping timer", self.session_id)
            self._stop_event.set()
            return

        # ── Capture screenshot ────────────────────────────────────────────────
        screenshot_path = self._capture_screenshot()
        if not screenshot_path:
            return

        svc.add_screenshot(session_id=self.session_id, screenshot_path=screenshot_path)

        # Refresh session after add
        session = svc.get_session(session_id=self.session_id)
        clue_history = list(session.get("clue_log") or [])
        screenshot_count = len(session.get("screenshot_paths") or [])

        # ── Run analyst agent ─────────────────────────────────────────────────
        analyst_result = self._run_analyst(
            screenshot_path=screenshot_path,
            clue_history=clue_history,
            screenshot_count=screenshot_count,
        )
        if analyst_result is None:
            return

        visible_clue = str(analyst_result.get("visible_clue") or "")
        best_guess = str(analyst_result.get("best_guess") or "")
        confidence = int(analyst_result.get("confidence") or 0)
        commentary = str(analyst_result.get("commentary") or "")

        # If the analyst flagged this screenshot as not showing a GeoGuessr scene
        # (e.g. user switched to chat/browser), skip the session update so the
        # accumulated confidence and best_guess are preserved unchanged.
        is_geoguessr_visible = bool(analyst_result.get("is_geoguessr_visible", True))
        if not is_geoguessr_visible:
            logger.debug(
                "[geo_timer] Non-game screenshot detected — preserving prior confidence/best_guess session=%s",
                self.session_id,
            )
            # Still emit a panel update (without clue/commentary) so the UI stays live,
            # but do NOT call svc.update_analysis — we want the confidence bar to hold.
            if self.socket_id:
                self._emit_update(
                    visible_clue="",
                    commentary="",
                    confidence=int(session.get("confidence") or 0),
                    screenshot_count=screenshot_count,
                    screenshot_path=screenshot_path,
                )
            return

        svc.update_analysis(
            session_id=self.session_id,
            best_guess=best_guess,
            confidence=confidence,
            clue=visible_clue,
        )

        # ── Proactive milestone chat ───────────────────────────────────────────
        # Announce once when confidence crosses meaningful thresholds so the
        # user knows Emi is making progress without waiting to ask.
        _MILESTONES = [
            (40, "Getting somewhere with this one..."),
            (70, "I have a pretty good feeling about where this is."),
            (90, "Yep. I know. Whenever you're ready."),
        ]
        for threshold, hint in _MILESTONES:
            if confidence >= threshold and threshold not in self._milestone_announced:
                self._milestone_announced.add(threshold)
                if self.socket_id:
                    self._emit_proactive_chat(hint)
                break  # only one announcement per tick

        # ── Emit geo_update to panel ──────────────────────────────────────────
        if self.socket_id:
            self._emit_update(
                visible_clue=visible_clue,
                commentary=commentary,
                confidence=confidence,
                screenshot_count=screenshot_count,
                screenshot_path=screenshot_path,
            )

        logger.info(
            "[geo_timer] tick done session=%s shot=%d confidence=%d",
            self.session_id,
            screenshot_count,
            confidence,
        )

    def _capture_screenshot(self) -> str | None:
        try:
            import ctypes
            from PIL import ImageGrab
            from datetime import datetime, timezone

            # Enumerate monitors
            monitors = _list_monitors_simple()
            if not monitors:
                logger.debug("[geo_timer] No monitors found")
                return None

            idx = min(self.monitor_index - 1, len(monitors) - 1)
            bbox = monitors[idx]

            from app.assistant.utils.path_utils import get_uploads_dir
            tmp_dir = get_uploads_dir() / "temp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"geo_{self.session_id}_{timestamp}_{uuid.uuid4().hex[:6]}.png"
            path = tmp_dir / filename

            try:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            except TypeError:
                img = ImageGrab.grab(bbox=bbox)
            img.save(path, format="PNG")
            logger.debug("[geo_timer] Captured screenshot %s", path)
            return str(path)
        except Exception:
            logger.debug("[geo_timer] screenshot capture failed", exc_info=True)
            return None

    def _run_analyst(
        self,
        *,
        screenshot_path: str,
        clue_history: list,
        screenshot_count: int,
    ) -> Dict[str, Any] | None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message, ScopeContext, ScopeResourcePolicy

            scope_context = ScopeContext(
                scope_id=f"geo_analyst::{self.session_id}",
                owner_id="master_room",
                actor_id="geo_screenshot_timer",
                surface="internal",
                room_id=self.room_id,
                room_context_id=self.context_id,
                resources=ScopeResourcePolicy(allowed_global_resources=["all"]),
            )
            agent = DI.agent_factory.create_agent("master_room::geoguessr_analyst")
            response = agent.action_handler(
                Message(
                    agent_input={
                        "geoguessr_screenshot": screenshot_path,
                        "geoguessr_clue_history": clue_history,
                        "geoguessr_screenshot_count": screenshot_count,
                    },
                    scope_context=scope_context,
                )
            )
            if response and response.data:
                return dict(response.data) if not isinstance(response.data, dict) else response.data
            logger.debug("[geo_timer] Analyst returned empty response")
            return None
        except Exception:
            logger.debug("[geo_timer] analyst agent failed", exc_info=True)
            return None

    def _emit_update(
        self,
        *,
        visible_clue: str,
        commentary: str,
        confidence: int,
        screenshot_count: int,
        screenshot_path: str,
    ) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()

            # Expose screenshot as a URL-accessible path under /uploads/temp/
            filename = Path(screenshot_path).name
            payload = {
                "data_type": "geo_update",
                "visible_clue": visible_clue,
                "commentary": commentary,
                "confidence": confidence,
                "screenshot_count": screenshot_count,
                "latest_screenshot": f"/uploads/temp/{filename}",
            }
            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": self.room_id}},
                user_message_data=UserMessageData(widget_data=[payload]),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
        except Exception:
            logger.debug("[geo_timer] emit_update failed", exc_info=True)
            raise

    def _emit_proactive_chat(self, text: str) -> None:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import UserMessage, UserMessageData
            assistant_name = get_required_assistant_name()

            msg = UserMessage(
                data_type="user_msg",
                sender=assistant_name,
                receiver=None,
                role="assistant",
                metadata={"reply_to": {"type": "socketio", "room_id": self.room_id, "tts_requested": self.tts_requested}},
                user_message_data=UserMessageData(
                    chat=text,
                    tts=self.tts_requested,
                    tts_text=text if self.tts_requested else None,
                ),
            )
            msg.event_topic = "socket_emit"
            DI.event_hub.publish(msg)
            logger.debug("[geo_timer] proactive chat emitted session=%s tts=%s: %s", self.session_id, self.tts_requested, text)
        except Exception:
            logger.debug("[geo_timer] emit_proactive_chat failed", exc_info=True)
            raise


# ── Monitor enumeration (lightweight, no BaseTool dependency) ──────────────

def _list_monitors_simple() -> list[tuple[int, int, int, int]]:
    """
    Returns list of (left, top, right, bottom) tuples for each monitor,
    sorted by left position. Falls back to full screen if enumeration fails.
    """
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong),
                    ("szDevice", ctypes.c_wchar * 32)]

    bboxes: list[tuple[int, int, int, int]] = []

    def _cb(h, hdc, lp, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if ctypes.windll.user32.GetMonitorInfoW(h, ctypes.byref(info)):
            r = info.rcMonitor
            bboxes.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return 1

    PROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                              ctypes.POINTER(RECT), ctypes.c_double)
    try:
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, PROC(_cb), 0)
        return sorted(bboxes, key=lambda b: (b[0], b[1]))
    except Exception:
        return [(0, 0, 1920, 1080)]


# ── Public API ─────────────────────────────────────────────────────────────

def start_game_timer(
    *,
    session_id: str,
    socket_id: str,
    room_id: str,
    surface: str,
    context_id: str,
    monitor_index: int = 1,
    tts_requested: bool = False,
) -> None:
    with _timers_lock:
        if session_id in _active_timers:
            logger.debug("[geo_timer] Timer already running for session=%s", session_id)
            return
        timer = _GeoScreenshotTimer(
            session_id=session_id,
            socket_id=socket_id,
            room_id=room_id,
            surface=surface,
            context_id=context_id,
            monitor_index=monitor_index,
            tts_requested=tts_requested,
        )
        _active_timers[session_id] = timer
        timer.start()


def stop_game_timer(*, session_id: str) -> None:
    with _timers_lock:
        timer = _active_timers.pop(session_id, None)
    if timer:
        timer.stop()


def pause_game_timer(*, session_id: str) -> None:
    with _timers_lock:
        timer = _active_timers.get(session_id)
    if timer:
        timer.pause()


def resume_game_timer(*, session_id: str) -> None:
    with _timers_lock:
        timer = _active_timers.get(session_id)
    if timer:
        timer.resume()


def reset_round_timer(*, session_id: str) -> None:
    """Resume timer after a next-round reset."""
    with _timers_lock:
        timer = _active_timers.get(session_id)
    if timer:
        timer.reset_round()
