"""PodClassifierService — subscribes to the gut, turns inbound envelopes into pods.

Two paths, dispatched by envelope shape:

**Chat burst path** (envelopes from UnifiedLogSource, source_type=unified_log):
  1. Buffered per room with a quiet-timer.
  2. On idle (>= ``quiet_threshold_seconds``), flush the room.
  3. Pass 1 (pod_classifier) → Pass 1.5 (pod_critic) → Pass 2 (pod_entity_resolver)
     → mint a single ``kind=chat_cluster`` pod whose body is the resolved sections.

**Email path** (envelopes from EmailRepoSource, signal_type=email):
  Each email is a single coherent unit, so no buffering — minted immediately
  on arrival as ``kind=email``. one_liner=subject, body=email body,
  source_refs point at event_repository:email by signal_id. Tags + LLM
  classification deferred to a follow-up pass; consumers filter by kind +
  sender for now.

Other signal_types are ignored for now — tool_result / resource_snapshot
pods will land via dedicated paths later.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from sqlalchemy import text

from app.assistant.ingest.contracts import IngestEnvelope
from app.assistant.pod_store.contracts import Pod, PodSourceRef
from app.assistant.pod_store.pod_store import PodStore
from app.assistant.runtime import start_monitored_thread
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.base import get_session

logger = get_logger(__name__)


_DEFAULT_QUIET_THRESHOLD_SECONDS = 300   # 5 min
_DEFAULT_TICK_INTERVAL_SECONDS = 60
_MAX_BURST_SIZE = 200  # safety cap — longer bursts get flushed immediately
# Pass 2 (pod_entity_resolver) uses a tight pre-context window — only
# enough to resolve implicit references like "the game", "she", "Bonnie".
# Keep it small so the entity-resolver prompt stays cheap.
_LOOKBACK_MAX_AGE_HOURS = 24
_LOOKBACK_MAX_MESSAGES = 20


def _iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return str(dt)


class PodClassifierService:
    """Gut subscriber that turns chat bursts into pods."""

    def __init__(
        self,
        *,
        pod_store: Optional[PodStore] = None,
        tag_vocabulary_path: Optional[Path] = None,
        quiet_threshold_seconds: int = _DEFAULT_QUIET_THRESHOLD_SECONDS,
        tick_interval_seconds: int = _DEFAULT_TICK_INTERVAL_SECONDS,
    ) -> None:
        self._pod_store = pod_store or PodStore()
        self._tag_vocabulary_path = tag_vocabulary_path or Path("configs/pod_tags.yaml")
        self._quiet_threshold_seconds = max(0, int(quiet_threshold_seconds))
        self._tick_interval_seconds = max(1, int(tick_interval_seconds))

        self._tag_vocabulary: Dict[str, Any] = self._load_tag_vocabulary()
        self._tag_to_agents: Dict[str, List[str]] = self._build_tag_subscription_map()

        # Per-room buffer of envelopes (chronological order of arrival) and
        # last-activity timestamp. Monotonic time is used for the timer.
        self._buffers: Dict[str, List[IngestEnvelope]] = defaultdict(list)
        self._last_activity_monotonic: Dict[str, float] = {}
        self._buffers_lock = threading.RLock()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

    # ──────────────────────────────────────────────────────────────────
    # Subscriber entrypoint (called by the gut)
    # ──────────────────────────────────────────────────────────────────

    def handle_envelope(self, envelope: IngestEnvelope) -> None:
        # Email envelopes are atomic — one email = one pod, no buffering.
        if self._is_email_envelope(envelope):
            try:
                self._process_email(envelope)
            except Exception as e:
                logger.error("PodClassifier: email processing failed signal_id=%s: %s",
                             getattr(envelope, "signal_id", "?"), e)
                logger.debug("email processing exception details", exc_info=True)
            return

        if not self._is_chat_envelope(envelope):
            return
        room_id = self._envelope_room_id(envelope)
        if not room_id:
            return
        with self._buffers_lock:
            self._buffers[room_id].append(envelope)
            self._last_activity_monotonic[room_id] = time.monotonic()
            # Force-flush oversize bursts so memory stays bounded regardless
            # of quiet timer.
            if len(self._buffers[room_id]) >= _MAX_BURST_SIZE:
                self._flush_room_locked(room_id, reason="max_burst_size")

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                logger.warning("PodClassifierService already running")
                return
            self._stop_event.clear()
            self._thread = start_monitored_thread(
                owner="pod_classifier",
                name="pod-classifier-tick",
                target=self._run_loop,
                daemon=True,
                kind="service_loop",
                metadata={
                    "component": "pod_classifier",
                    "quiet_threshold_seconds": self._quiet_threshold_seconds,
                    "tick_interval_seconds": self._tick_interval_seconds,
                    "tag_count": len(self._tag_vocabulary),
                },
            )
        logger.info(
            "PodClassifierService started pid=%s tick=%ss quiet=%ss tags=%s",
            os.getpid(),
            self._tick_interval_seconds,
            self._quiet_threshold_seconds,
            list(self._tag_vocabulary.keys()),
        )

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread:
            thread.join(timeout=2.0)
        logger.info("PodClassifierService stopped")

    def tick_once(self) -> int:
        """Run one quiet-timer sweep across all buffered rooms. Exposed
        for tests and for callers that want to drive flushing manually.
        Returns the number of rooms flushed."""
        return self._sweep_and_flush()

    # ──────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                flushed = self._sweep_and_flush()
                if flushed:
                    logger.debug("PodClassifier tick flushed %d room(s)", flushed)
            except Exception as e:
                logger.error("PodClassifier tick iteration failed: %s", e)
                logger.debug("pod classifier tick exception details", exc_info=True)
            self._stop_event.wait(self._tick_interval_seconds)

    def _sweep_and_flush(self) -> int:
        now_mono = time.monotonic()
        to_flush: List[str] = []
        with self._buffers_lock:
            for room_id, last in self._last_activity_monotonic.items():
                if not self._buffers.get(room_id):
                    continue
                if now_mono - last >= self._quiet_threshold_seconds:
                    to_flush.append(room_id)
        # Flush outside the buffer lock where possible (we re-take inside).
        count = 0
        for room_id in to_flush:
            with self._buffers_lock:
                if self._buffers.get(room_id):
                    self._flush_room_locked(room_id, reason="quiet_timeout")
                    count += 1
        return count

    def _flush_room_locked(self, room_id: str, *, reason: str) -> None:
        """Pop buffered envelopes for a room and process them. Caller must
        hold ``_buffers_lock``. Releases the envelopes before calling the
        classifier so new arrivals during classification land in the next
        window."""
        envelopes = list(self._buffers.get(room_id) or [])
        self._buffers[room_id] = []
        self._last_activity_monotonic.pop(room_id, None)
        if not envelopes:
            return

        logger.info(
            "PodClassifier flushing room=%s reason=%s envelope_count=%d",
            room_id, reason, len(envelopes),
        )
        # Run classification and minting outside the buffer lock.
        # We release by spawning — but the lock is reentrant and the
        # expensive path is just a fn call; simpler to just release now.
        # NOTE: we already cleared the buffer above, so releasing is safe.
        try:
            self._process_burst(room_id=room_id, envelopes=envelopes)
        except Exception as e:
            logger.error("PodClassifier: flush failed for room=%s: %s", room_id, e)
            logger.debug("PodClassifier flush exception details", exc_info=True)

    def _process_burst(self, *, room_id: str, envelopes: List[IngestEnvelope]) -> None:
        burst_text = self._render_burst(envelopes)
        if not burst_text.strip():
            return

        # Pass 1: classify the burst in isolation. No chat history is passed
        # here — prevents the LLM from leaking lookback content into the
        # sections, and keeps the classification prompt small/fast.
        classification = self._classify_burst(burst_text)
        tags = classification.get("tags") or []
        one_liner = str(classification.get("one_liner") or "").strip()
        sections = classification.get("sections") or []
        if not tags or not one_liner:
            logger.info(
                "PodClassifier room=%s classifier returned no tags — minting nothing",
                room_id,
            )
            return

        raw_sections: List[str] = []
        if isinstance(sections, list):
            raw_sections = [str(s).strip() for s in sections if str(s).strip()]

        if not raw_sections:
            logger.info(
                "PodClassifier room=%s Pass 1 returned tags but no sections — skipping",
                room_id,
            )
            return

        pre_context_text = self._fetch_chat_history(
            room_id=room_id,
            burst_envelope_ids={str(e.signal_id) for e in envelopes if e.signal_id},
        )

        # Pass 1.5: critic gate. Rejects pods with no substance, derivative
        # recaps, third-party chatter, or content that belongs in KG/beliefs.
        # Skipping Pass 2 on rejection saves the entity-resolver call.
        verdict = self._critique_pod(
            one_liner=one_liner,
            tags=tags,
            sections=raw_sections,
            pre_context=pre_context_text,
        )
        if not verdict.get("accept"):
            logger.info(
                "PodClassifier room=%s critic REJECTED pod (reason=%s) — minting nothing",
                room_id, verdict.get("reason", ""),
            )
            return

        # Pass 2: entity resolution on the kept sections.
        annotated_sections = self._resolve_entities(
            sections=raw_sections,
            pre_context=pre_context_text,
        )

        pod_body = "\n\n".join(annotated_sections) if annotated_sections else burst_text

        for_agents = self._compute_for_agents(tags)
        pod_id = self._make_cluster_pod_id(envelopes)
        source_refs = [
            PodSourceRef(kind="unified_log", id=str(env.signal_id))
            for env in envelopes
            if env.signal_id
        ]
        pod = Pod(
            pod_id=pod_id,
            kind="chat_cluster",
            tags=list(tags),
            one_liner=one_liner,
            body=pod_body,
            source_refs=source_refs,
            for_agents=for_agents,
            scope_id=room_id,
            created_by="pod_classifier",
            metadata={
                "burst_envelope_count": len(envelopes),
                "classifier_reasoning": str(classification.get("reasoning") or ""),
                "critic_content_type": str(verdict.get("content_type") or ""),
                "critic_reason": str(verdict.get("reason") or ""),
            },
        )
        self._pod_store.put(pod)
        logger.info(
            "PodClassifier minted pod id=%s room=%s tags=%s for_agents=%s size=%d",
            pod_id, room_id, tags, for_agents, len(envelopes),
        )

    # ──────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_chat_envelope(envelope: IngestEnvelope) -> bool:
        # Chat envelopes come from UnifiedLogSource with source_type=unified_log.
        return str(getattr(envelope, "source_type", "")).strip() == "unified_log"

    @staticmethod
    def _is_email_envelope(envelope: IngestEnvelope) -> bool:
        # Emails come from EmailRepoSource with signal_type=email.
        return str(getattr(envelope, "signal_type", "")).strip() == "email"

    def _process_email(self, envelope: IngestEnvelope) -> None:
        """Mint a single email pod from one envelope.

        Single-shot — no buffering, no LLM classification in v1. The email is
        already an atomic unit with structured metadata (subject, sender,
        body); tag inference can come in a follow-up pass without blocking
        consumers that just want to find an email by sender or recipient.
        """
        signal_id = str(getattr(envelope, "signal_id", "") or "").strip()
        if not signal_id:
            logger.debug("PodClassifier: email envelope missing signal_id, skipping")
            return

        # Reject duplicates: signal_id is deterministic for an email row, so
        # if a pod with the matching source_ref already exists we've already
        # minted it. Cheap dedup via PodStore.get on the deterministic id.
        pod_id = self._make_email_pod_id(signal_id)
        if self._pod_store.get(pod_id) is not None:
            logger.debug("PodClassifier: email pod %s already exists, skipping", pod_id[:24])
            return

        meta = envelope.metadata if isinstance(envelope.metadata, dict) else {}
        data = envelope.data if isinstance(envelope.data, dict) else {}

        subject = str(meta.get("subject") or data.get("subject") or "").strip()
        sender_display = str(meta.get("sender_display") or data.get("sender") or "").strip()
        sender_email = str(meta.get("sender_email") or data.get("email_address") or "").strip()
        account_id = str(meta.get("account_id") or data.get("account_id") or "").strip()
        body = str(data.get("body") or "").strip()
        summary = str(data.get("summary") or "").strip()

        # one_liner: prefer "Sender: subject" if both present, else whichever
        # is non-empty. Subject alone is the most common useful header.
        if sender_display and subject:
            one_liner = f"{sender_display}: {subject}"
        elif subject:
            one_liner = subject
        elif sender_display:
            one_liner = f"email from {sender_display}"
        elif summary:
            one_liner = summary[:140]
        else:
            one_liner = "email (no subject)"

        # Body: prefer the full email text; fall back to the summary if body
        # is empty (e.g., header-only sync). The whole point of pods is that
        # consumers fetch the body on demand, so storing the full text is fine.
        pod_body = body or summary or ""

        pod = Pod(
            pod_id=pod_id,
            kind="email",
            tags=[],  # tag inference deferred to a follow-up pass
            one_liner=one_liner,
            body=pod_body,
            source_refs=[
                PodSourceRef(kind="event_repository:email", id=signal_id),
            ],
            for_agents=[],  # consumers filter by kind="email" + sender for now
            scope_id=account_id or None,
            created_by="pod_classifier",
            metadata={
                "subject": subject,
                "sender_display": sender_display,
                "sender_email": sender_email,
                "account_id": account_id,
                "uid": str(meta.get("uid") or ""),
                "occurred_at_utc": str(envelope.occurred_at_utc or ""),
            },
        )
        self._pod_store.put(pod)
        logger.info(
            "PodClassifier minted email pod id=%s sender=%r subject=%r account=%s",
            pod_id, sender_email or sender_display, subject[:80], account_id,
        )

    @staticmethod
    def _make_email_pod_id(signal_id: str) -> str:
        """Deterministic pod_id from the email's signal_id.

        Mirrors _make_cluster_pod_id's shape (datapod:<kind>:<24-hex>) so the
        existing POD_URI_RE matches both. Will be shortened to 6 base36 once
        the cluster path is.
        """
        digest = hashlib.sha256(signal_id.encode("utf-8")).hexdigest()[:24]
        return f"datapod:email:{digest}"

    @staticmethod
    def _envelope_room_id(envelope: IngestEnvelope) -> str:
        meta = envelope.metadata if isinstance(envelope.metadata, dict) else {}
        rid = str(meta.get("room_id") or "").strip()
        return rid

    @staticmethod
    def _render_burst(envelopes: List[IngestEnvelope]) -> str:
        """Render burst in the same ``- [time] Speaker: content`` format as
        the CHAT HISTORY section. Identical formatting lets the classifier
        copy lines verbatim without reformatting them — the validator then
        sees exact string matches."""
        from app.assistant.utils.time_utils import utc_to_local
        lines: List[str] = []
        for env in envelopes:
            content = str(env.content or "").strip()
            if not content:
                continue
            meta = env.metadata if isinstance(env.metadata, dict) else {}
            speaker = (
                str(meta.get("speaker_name") or "").strip()
                or str(env.source_id or "").strip()
                or "Unknown"
            )
            time_label = ""
            raw_ts = str(env.occurred_at_utc or "").strip()
            if raw_ts:
                try:
                    from datetime import datetime
                    parsed = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    time_label = utc_to_local(parsed).strftime("%I:%M %p").lstrip("0")
                except Exception:
                    time_label = ""
            prefix = f"- [{time_label}] " if time_label else "- "
            lines.append(f"{prefix}{speaker}: {content}")
        return "\n".join(lines)

    def _classify_burst(self, burst_text: str) -> Dict[str, Any]:
        from app.assistant.ServiceLocator.service_locator import DI
        agent = DI.agent_factory.create_agent("pod_classifier")
        if agent is None:
            logger.error("PodClassifier: agent_factory returned None for pod_classifier")
            return {}
        result = agent.action_handler(
            Message(
                agent_input={
                    "burst_content": burst_text,
                    "tag_vocabulary": self._tag_vocabulary,
                }
            )
        )
        data = result.data if isinstance(getattr(result, "data", None), dict) else {}
        return data

    def _critique_pod(
        self,
        *,
        one_liner: str,
        tags: List[str],
        sections: List[str],
        pre_context: str,
    ) -> Dict[str, Any]:
        """Pass 1.5: critic gate. Returns dict with accept/content_type/reason.
        On any failure, defaults to accept=True so pipeline errors don't
        silently suppress pods — fail open, not closed."""
        from app.assistant.ServiceLocator.service_locator import DI
        try:
            agent = DI.agent_factory.create_agent("pod_critic")
            if agent is None:
                logger.error("PodClassifier: agent_factory returned None for pod_critic")
                return {"accept": True, "content_type": "", "reason": "critic_unavailable"}
            result = agent.action_handler(
                Message(
                    agent_input={
                        "one_liner": one_liner,
                        "tags": list(tags),
                        "sections": list(sections),
                        "sections_text": "\n\n".join(sections),
                        "pre_context": pre_context or "",
                    }
                )
            )
            data = result.data if isinstance(getattr(result, "data", None), dict) else {}
            return {
                "accept": bool(data.get("accept")),
                "content_type": str(data.get("content_type") or ""),
                "reason": str(data.get("reason") or ""),
            }
        except Exception as e:
            logger.error("PodClassifier: critic failed: %s", e)
            logger.debug("PodClassifier critic exception details", exc_info=True)
            return {"accept": True, "content_type": "", "reason": f"critic_error: {e}"}

    def _resolve_entities(
        self,
        *,
        sections: List[str],
        pre_context: str,
    ) -> List[str]:
        """Pass 2: call pod_entity_resolver to insert entity parentheticals.
        Falls back to the raw input sections if the resolver fails or
        returns a mismatched length."""
        if not sections:
            return sections
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            agent = DI.agent_factory.create_agent("pod_entity_resolver")
            if agent is None:
                logger.error("PodClassifier: agent_factory returned None for pod_entity_resolver")
                return sections
            result = agent.action_handler(
                Message(
                    agent_input={
                        "sections": sections,
                        "sections_text": "\n\n".join(sections),
                        "pre_context": pre_context or "",
                    }
                )
            )
            data = result.data if isinstance(getattr(result, "data", None), dict) else {}
            annotated = data.get("annotated_sections") or []
            if not isinstance(annotated, list) or len(annotated) != len(sections):
                logger.warning(
                    "PodClassifier: pod_entity_resolver returned %d sections, expected %d — falling back to raw",
                    len(annotated) if isinstance(annotated, list) else -1,
                    len(sections),
                )
                return sections
            cleaned = [str(s).strip() for s in annotated]
            # Final guard: keep resolver output only if non-empty; otherwise
            # pass through the raw section.
            return [
                (ann if ann else raw)
                for ann, raw in zip(cleaned, sections)
            ]
        except Exception as e:
            logger.error("PodClassifier: entity resolver failed: %s", e)
            logger.debug("PodClassifier entity resolver exception details", exc_info=True)
            return sections

    def _fetch_chat_history(
        self,
        *,
        room_id: str,
        burst_envelope_ids: set[str],
    ) -> str:
        """Load prior room history via RoomHistoryBuilder — the SAME path
        master_room chat_gate uses. Returns a timestamped transcript that
        the classifier reads as the CHAT HISTORY section. The burst's own
        envelopes are excluded so they don't appear twice (burst is shown
        again separately in BURST TO EVALUATE).
        """
        try:
            from app.assistant.room_session_manager.services.room_history_builder import (
                RoomHistoryBuilder,
            )
            from app.assistant.utils.time_utils import utc_to_local
            builder = RoomHistoryBuilder()
            messages = builder.load_room_history_messages(
                room_id=room_id,
                max_age_hours=_LOOKBACK_MAX_AGE_HOURS,
            )
        except Exception as e:
            logger.error("PodClassifier: chat history load failed for room=%s: %s", room_id, e)
            logger.debug("PodClassifier chat history exception details", exc_info=True)
            return ""

        if not messages:
            return ""

        filtered = [
            m for m in messages
            if str(getattr(m, "id", "") or "") not in burst_envelope_ids
        ]
        if not filtered:
            return ""

        if len(filtered) > _LOOKBACK_MAX_MESSAGES:
            filtered = filtered[-_LOOKBACK_MAX_MESSAGES:]

        lines: List[str] = []
        for m in filtered:
            speaker = (
                str(getattr(m, "room_speaker_name", None) or "").strip()
                or str(getattr(m, "sender", None) or "").strip()
                or str(getattr(m, "role", None) or "").strip()
                or "Unknown"
            )
            content = str(getattr(m, "content", None) or "").strip()
            if not content:
                continue
            ts = getattr(m, "timestamp", None)
            time_label = ""
            if ts is not None:
                try:
                    time_label = utc_to_local(ts).strftime("%I:%M %p").lstrip("0")
                except Exception:
                    time_label = ""
            prefix = f"- [{time_label}] " if time_label else "- "
            lines.append(f"{prefix}{speaker}: {content}")
        return "\n".join(lines)

    def _compute_for_agents(self, tags: List[str]) -> List[str]:
        agents: List[str] = []
        seen: set[str] = set()
        for tag in tags:
            for agent_name in self._tag_to_agents.get(tag, []):
                if agent_name not in seen:
                    seen.add(agent_name)
                    agents.append(agent_name)
        return agents

    @staticmethod
    def _make_cluster_pod_id(envelopes: List[IngestEnvelope]) -> str:
        """Deterministic pod_id from the sorted signal_ids of the envelopes."""
        signal_ids = sorted(str(env.signal_id) for env in envelopes if env.signal_id)
        payload = "|".join(signal_ids)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return f"datapod:chat_cluster:{digest}"

    def _load_tag_vocabulary(self) -> Dict[str, Any]:
        path = self._tag_vocabulary_path
        if not path.exists():
            logger.error("PodClassifier: tag vocabulary file missing: %s", path)
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            tags = data.get("tags", {}) if isinstance(data, dict) else {}
            return tags if isinstance(tags, dict) else {}
        except Exception as e:
            logger.error("PodClassifier: failed loading tag vocabulary: %s", e)
            logger.debug("pod classifier tag load exception details", exc_info=True)
            return {}

    def _build_tag_subscription_map(self) -> Dict[str, List[str]]:
        """Scan agent registry for pod_interest.tags declarations.

        Returns dict of tag_name -> [agent_name, ...]. Empty map is fine —
        it just means no consumer has registered yet, so pods are minted
        with an empty for_agents list until consumers declare interest.
        """
        mapping: Dict[str, List[str]] = defaultdict(list)
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            registry = DI.agent_registry
        except Exception:
            logger.debug("PodClassifier: agent_registry unavailable, skipping interest scan")
            return {}
        try:
            names = registry.get_all_agent_names() if hasattr(registry, "get_all_agent_names") else []
        except Exception as e:
            logger.error("PodClassifier: could not list agent names: %s", e)
            return {}
        for name in names:
            cfg = registry.get_agent_config(name) if hasattr(registry, "get_agent_config") else None
            if not isinstance(cfg, dict):
                continue
            pod_interest = cfg.get("pod_interest")
            if not isinstance(pod_interest, dict):
                continue
            tags = pod_interest.get("tags")
            if not isinstance(tags, list):
                continue
            for t in tags:
                if isinstance(t, str) and t.strip():
                    mapping[t.strip()].append(name)
        if mapping:
            logger.info(
                "PodClassifier tag subscriptions: %s",
                {k: v for k, v in mapping.items()},
            )
        else:
            logger.info("PodClassifier: no agents declared pod_interest yet")
        return dict(mapping)
