from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from app.assistant.utils.logging_config import get_logger
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.dj_manager.scope import build_dj_scope_context

logger = get_logger(__name__)

DEFAULT_PLAN_DURATION_MINUTES = 60
VIBE_RECHECK_MINUTES = 30
VIBE_FAILURE_BACKOFF_SECONDS = 60


class VibePlanner:
    def __init__(self):
        self._plan: Optional[Dict[str, Any]] = None
        self._plan_start_utc: Optional[datetime] = None
        self._last_check_utc: Optional[datetime] = None
        # Track chat changes separately from "planned successfully" to avoid retry spam on failures.
        self._last_chat_sig_seen: Optional[str] = None
        self._last_chat_sig_planned: Optional[str] = None
        self._last_failure_utc: Optional[datetime] = None

    def clear(self) -> None:
        self._plan = None
        self._plan_start_utc = None
        self._last_check_utc = None
        self._last_chat_sig_seen = None
        self._last_chat_sig_planned = None
        self._last_failure_utc = None

    def get_plan_debug(self) -> Optional[Dict[str, Any]]:
        if not self._plan:
            return None

        now = datetime.now(timezone.utc)
        elapsed = 0.0
        if self._plan_start_utc:
            elapsed = (now - self._plan_start_utc).total_seconds() / 60.0

        return {
            "verbal_plan": self._plan.get("verbal_plan", ""),
            "context_block": self._plan.get("current_context_block", ""),
            "plan_duration_minutes": self._plan.get("plan_duration_minutes", 0),
            "elapsed_minutes": round(elapsed, 1),
            "phases": len(self._plan.get("phases", [])),
            "last_vibe_check": self._last_check_utc.isoformat() if self._last_check_utc else None,
            "music_filters": self._plan.get("music_filters"),
        }

    def ensure_fresh_plan(self, calendar_events: list, recent_chat: list) -> None:
        """
        Ensure we have a fresh vibe plan.

        Triggers a recheck when:
        - no plan / plan expired / periodic interval
        - recent chat changed (new user input should change the vibe immediately)
        """
        chat_sig = self._chat_signature(recent_chat)
        if chat_sig and chat_sig != self._last_chat_sig_seen:
            # New chat content since last check; force recheck.
            logger.info("Vibe recheck triggered by new chat")
        elif not self._needs_vibe_check():
            return

        now_utc = datetime.now(timezone.utc)

        # Backoff if vibe_check is failing repeatedly, to avoid hammering the LLM.
        if self._last_failure_utc is not None and (now_utc - self._last_failure_utc).total_seconds() < VIBE_FAILURE_BACKOFF_SECONDS:
            return

        # Mark chat as "seen" even if the agent fails, so we don't spam rechecks on every call.
        self._last_chat_sig_seen = chat_sig
        self._last_check_utc = now_utc

        plan = self._call_vibe_check(calendar_events=calendar_events, recent_chat=recent_chat)
        if plan:
            self._plan = plan
            self._plan_start_utc = now_utc
            self._last_check_utc = now_utc
            self._last_chat_sig_planned = chat_sig
            self._last_failure_utc = None
        else:
            self._last_failure_utc = now_utc

    def _chat_signature(self, recent_chat: list) -> Optional[str]:
        """
        Create a small stable signature of recent chat so we can detect changes.
        Avoids storing full content.
        """
        if not isinstance(recent_chat, list) or not recent_chat:
            return None
        try:
            import hashlib

            # Use last few messages; prefer stable keys if present.
            tail = recent_chat[-5:]
            parts = []
            for m in tail:
                if not isinstance(m, dict):
                    continue
                t = str(m.get("time_utc") or m.get("time_local") or "")
                sender = str(m.get("sender") or "")
                content = str(m.get("content") or "")
                parts.append(f"{t}|{sender}|{content[:200]}")
            joined = "\n".join(parts)
            if not joined:
                return None
            return hashlib.sha1(joined.encode("utf-8", errors="replace")).hexdigest()
        except Exception:
            # If anything goes wrong, don't block planning.
            return None

    def get_targets(self) -> Dict[str, Any]:
        """
        Return current targets by interpolating the active plan.

        Primary output: audio_targets (0-100 sliders).
        Temporary legacy output (until DJ prompt + history schema are migrated):
        - energy_target (1-10)
        - valence_target (-1..+1)
        - vocal_tolerance (1-10) derived from instrumentalness
        """
        if not self._plan or not self._plan_start_utc:
            audio = self._default_audio_targets()
            return self._decorate_targets(audio_targets=audio, phase_note="", phase_progress=0.0)

        now = datetime.now(timezone.utc)
        elapsed_minutes = (now - self._plan_start_utc).total_seconds() / 60.0

        phases = self._plan.get("phases", []) or []
        if not phases:
            audio = self._default_audio_targets()
            return self._decorate_targets(audio_targets=audio, phase_note="", phase_progress=0.0)

        phase_start = 0.0
        current_phase = phases[0]
        for phase in phases:
            phase_duration = float(phase.get("duration_minutes", 30) or 0)
            if elapsed_minutes < phase_start + phase_duration:
                current_phase = phase
                break
            phase_start += phase_duration
        else:
            current_phase = phases[-1]
            phase_start = sum(float(p.get("duration_minutes", 0) or 0) for p in phases[:-1])

        phase_duration = float(current_phase.get("duration_minutes", 30) or 0)
        phase_elapsed = elapsed_minutes - phase_start
        progress = 0.0
        if phase_duration > 0:
            progress = min(1.0, max(0.0, phase_elapsed / phase_duration))

        audio = self._interpolate_audio_targets(current_phase=current_phase, progress=progress)
        return self._decorate_targets(
            audio_targets=audio,
            phase_note=current_phase.get("note", ""),
            phase_progress=round(progress, 2),
        )

    def _default_audio_targets(self) -> Dict[str, int]:
        # Reasonable default for a "neutral focus-ish" vibe.
        return {
            "energy": 55,
            "valence": 50,
            "loudness": 55,
            "speechiness": 10,
            "acousticness": 40,
            "instrumentalness": 70,
            "liveness": 15,
            "tempo": 45,
        }

    def _interpolate_audio_targets(self, current_phase: Dict[str, Any], progress: float) -> Dict[str, int]:
        """
        Interpolate 0-100 audio sliders for the current phase.

        Supported phase formats:
        - Hold: {"targets": {...}}
        - Gradient: {"targets_start": {...}, "targets_end": {...}}
        """
        defaults = self._default_audio_targets()

        def clamp01(x: float) -> float:
            return max(0.0, min(1.0, float(x)))

        def clamp100(x: Any, fallback: int) -> int:
            try:
                return int(round(max(0.0, min(100.0, float(x)))))
            except Exception:
                return fallback

        if isinstance(current_phase.get("targets"), dict):
            t = current_phase.get("targets") or {}
            return {k: clamp100(t.get(k), defaults[k]) for k in defaults.keys()}

        start = current_phase.get("targets_start") if isinstance(current_phase.get("targets_start"), dict) else None
        end = current_phase.get("targets_end") if isinstance(current_phase.get("targets_end"), dict) else None
        if not start or not end:
            return defaults

        p = clamp01(progress)
        out: Dict[str, int] = {}
        for k in defaults.keys():
            a = clamp100(start.get(k), defaults[k])
            b = clamp100(end.get(k), defaults[k])
            out[k] = clamp100(a + (b - a) * p, defaults[k])
        return out

    def _decorate_targets(self, audio_targets: Dict[str, int], phase_note: str, phase_progress: float) -> Dict[str, Any]:
        """
        Add plan/context fields and temporary legacy mappings.
        """
        try:
            from app.assistant.dj_manager.feature_scaler import valence_slider_to_signed
        except Exception:
            valence_slider_to_signed = None

        energy_slider = float(audio_targets.get("energy", 55))
        valence_slider = float(audio_targets.get("valence", 50))
        instrumentalness_slider = float(audio_targets.get("instrumentalness", 70))

        # Optional "high-level" knob from vibe_check (if present); otherwise derive.
        intensity = None
        try:
            raw_intensity = self._plan.get("intensity") if isinstance(self._plan, dict) else None
            if raw_intensity is not None:
                intensity = int(round(max(0.0, min(100.0, float(raw_intensity)))))
        except Exception:
            intensity = None
        if intensity is None:
            # Derive a reasonable proxy from the main "intensity-like" sliders.
            try:
                loud = float(audio_targets.get("loudness", 55))
            except Exception:
                loud = 55.0
            try:
                tempo = float(audio_targets.get("tempo", 45))
            except Exception:
                tempo = 45.0
            intensity = int(round(max(0.0, min(100.0, (energy_slider + loud + tempo) / 3.0))))

        # Legacy mappings (temporary)
        energy_1_10 = int(round(1 + (max(0.0, min(100.0, energy_slider)) / 100.0) * 9))
        if valence_slider_to_signed:
            valence_signed = float(valence_slider_to_signed(valence_slider))
        else:
            valence_signed = ((max(0.0, min(100.0, valence_slider)) / 100.0) * 2.0) - 1.0

        vocal_tol = int(round(1 + ((100.0 - max(0.0, min(100.0, instrumentalness_slider))) / 100.0) * 9))
        vocal_tol = max(1, min(10, vocal_tol))

        return {
            # Primary (new) targets
            "audio_targets": {k: int(audio_targets[k]) for k in audio_targets.keys()},

            # Temporary legacy targets
            "energy_target": energy_1_10,
            "valence_target": round(valence_signed, 2),
            "vocal_tolerance": vocal_tol,

            # Extra high-level knob
            "intensity": int(intensity),

            # Plan/context metadata
            "context_block": self._plan.get("current_context_block", "unknown") if self._plan else "unknown",
            "verbal_plan": self._plan.get("verbal_plan", "") if self._plan else "",
            "phase_note": phase_note,
            "phase_progress": phase_progress,
            "current_mood": self._plan.get("current_mood", "unknown") if self._plan else "unknown",
            "current_energy": self._plan.get("current_energy", "unknown") if self._plan else "unknown",
            "anxiety_level": self._plan.get("anxiety_level", "calm") if self._plan else "calm",
            "music_filters": self._plan.get("music_filters") if self._plan else None,
        }

    def _needs_vibe_check(self) -> bool:
        now = datetime.now(timezone.utc)

        if self._plan is None or self._plan_start_utc is None:
            return True

        # Periodic recheck (even if plan is still "valid"), to incorporate new context over time.
        if self._last_check_utc is None:
            return True
        if (now - self._last_check_utc) >= timedelta(minutes=VIBE_RECHECK_MINUTES):
            return True

        elapsed = (now - self._plan_start_utc).total_seconds() / 60.0
        plan_duration = float(self._plan.get("plan_duration_minutes", DEFAULT_PLAN_DURATION_MINUTES) or DEFAULT_PLAN_DURATION_MINUTES)
        if elapsed >= plan_duration:
            return True

        return False

    def _call_vibe_check(self, calendar_events: list, recent_chat: list) -> Optional[Dict[str, Any]]:
        try:
            from app.assistant.utils.pydantic_classes import Message
            from app.assistant.utils.time_utils import get_local_time
        except Exception as e:
            logger.error("Could not import vibe_check dependencies: %s", e)
            logger.debug("could not import vibe_check dependencies exception details", exc_info=True)
            return None

        try:
            now_local = get_local_time()
            now_utc = datetime.now(timezone.utc)

            previous_state = None
            if self._plan and self._plan_start_utc:
                elapsed = (now_utc - self._plan_start_utc).total_seconds() / 60.0
                current_targets = self.get_targets()
                previous_state = {
                    "verbal_plan": self._plan.get("verbal_plan", ""),
                    "context_block": self._plan.get("current_context_block", ""),
                    "plan_duration_minutes": self._plan.get("plan_duration_minutes", 0),
                    "elapsed_minutes": round(elapsed, 1),
                    "current_targets": current_targets.get("audio_targets"),
                    "current_phase_note": current_targets.get("phase_note", ""),
                    "music_filters": self._plan.get("music_filters"),
                }

            agent_input = {
                "day_of_week": now_local.strftime("%A"),
                "calendar_events": calendar_events,
                "recent_chat": recent_chat,
                "previous_state": previous_state,
            }

            agent = DI.agent_factory.create_agent("vibe_check")
            result = agent.action_handler(
                Message(
                    agent_input=agent_input,
                    scope_context=build_dj_scope_context(actor_id="vibe_check_runner"),
                )
            )

            if hasattr(result, "data") and isinstance(result.data, dict):
                return result.data
            if isinstance(result, dict):
                return result
            return None
        except Exception as e:
            logger.error("Error calling vibe_check agent: %s", e)
            logger.debug("error calling vibe_check agent exception details", exc_info=True)
            return None
