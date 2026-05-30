"""
Stage 3: Critique + extract (combined).

For each kg_window not yet in kg_window_extraction:
  1. Run window_critic on segment's user/context lines.
  2. If critic rejects → write extraction row with verdict='rejected'.
  3. Else → run fact_extractor on resolved user text.
  4. Write extraction row with verdict='extracted' + nodes/edges JSON.

The two LLM calls share one input/output bucket — critic verdict is only
consumed by the extractor, so they're combined.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.assistant.database.db_handler import UnifiedLog2026
from app.assistant.database.kg_pipeline_models import (
    KGResolvedMessage,
    KGWindow,
    KGWindowExtraction,
    KGWindowMessage,
)
from app.assistant.pipelines.kg_shared import normalize_extraction_result
from app.assistant.scope.loader import load_scope_for_source
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.models.db_manager import get_db_manager

logger = get_logger(__name__)

WINDOW_CRITIC_AGENT_NAME = "knowledge_graph_add::window_critic_v2"
FACT_EXTRACTOR_AGENT_NAME = "knowledge_graph_add::fact_extractor"
EXTRACTOR_VERSION = "kg_pipeline_v1"
CRITIC_VERSION = "kg_pipeline_v2"

MAX_SEGMENTS_PER_CYCLE = 60


class CritiqueAndExtractStep:
    name = "critique_and_extract"

    def _claim_pending_window_ids(self, limit: int) -> List[str]:
        with get_db_manager().read_session() as session:
            extracted_ids = session.query(KGWindowExtraction.window_id).subquery().select()
            rows = (
                session.query(KGWindow.id)
                .filter(~KGWindow.id.in_(extracted_ids))
                .order_by(KGWindow.start_timestamp.asc(), KGWindow.id.asc())
                .limit(limit)
                .all()
            )
            return [rid for (rid,) in rows]

    def pending_count(self) -> int:
        from sqlalchemy import func as sqlfunc
        with get_db_manager().read_session() as session:
            extracted_ids = session.query(KGWindowExtraction.window_id).subquery().select()
            return int(
                session.query(sqlfunc.count(KGWindow.id))
                .filter(~KGWindow.id.in_(extracted_ids))
                .scalar() or 0
            )

    def _load_window_lines(self, window_id: str) -> Optional[Dict[str, Any]]:
        """Return {user_lines, context_lines, user_text, full_conversation,
        message_timestamp} or None if no usable content."""
        with get_db_manager().read_session() as session:
            seg = session.query(KGWindow).filter(KGWindow.id == window_id).first()
            if seg is None:
                return None

            rows = (
                session.query(
                    KGWindowMessage.item_order,
                    UnifiedLog2026.role,
                    UnifiedLog2026.speaker_name,
                    KGResolvedMessage.resolved_text,
                    UnifiedLog2026.message,
                )
                .join(UnifiedLog2026, UnifiedLog2026.id == KGWindowMessage.unified_log_id)
                .outerjoin(KGResolvedMessage, KGResolvedMessage.unified_log_id == KGWindowMessage.unified_log_id)
                .filter(KGWindowMessage.window_id == window_id)
                .order_by(KGWindowMessage.item_order.asc())
                .all()
            )

            user_lines: List[str] = []
            context_lines: List[str] = []
            full_lines: List[str] = []
            for r in rows:
                _, role, speaker, resolved, raw = r
                role = (role or "").strip().lower()
                text = (resolved or raw or "").strip()
                if not text:
                    continue
                speaker = (speaker or "").strip() or role or "user"
                if role == "user":
                    user_lines.append(f"{speaker}: {text}")
                    full_lines.append(f"User ({speaker}): {text}")
                elif role == "assistant":
                    context_lines.append(text)
                    full_lines.append(f"Assistant: {text}")

            if not user_lines:
                return None

            return {
                "user_lines": user_lines,
                "context_lines": context_lines,
                "user_text": "\n".join(user_lines),
                "full_conversation": "\n".join(full_lines),
                "message_timestamp": seg.end_timestamp.isoformat() if seg.end_timestamp else None,
            }

    def _call_window_critic(
        self, user_lines: List[str], context_lines: List[str],
    ) -> Dict[str, Any]:
        """Run window_critic_v2 and return its line-item verdict.

        Returns:
            {
              "extractable": bool,        # at least one user line marked claim-bearing
              "kept_indices": [int, ...], # 0-based indices into user_lines
              "reason": str,              # overall_reason from v2
            }
        On any failure → defaults to accept-all (every user line extractable)
        so the extractor still runs, matching v1's fail-open behaviour.
        """
        from app.assistant.ServiceLocator.service_locator import DI

        context_block = "\n".join(context_lines) if context_lines else "(no assistant context)"
        indexed_block = "\n".join(f"[{i}] {line}" for i, line in enumerate(user_lines)) \
            if user_lines else "(no user lines)"
        all_indices = list(range(len(user_lines)))

        try:
            agent = DI.agent_factory.create_agent(WINDOW_CRITIC_AGENT_NAME)
            if not agent:
                return {"extractable": True, "kept_indices": all_indices,
                        "reason": "critic agent unavailable"}
            scope = load_scope_for_source(
                kind="pipeline", source_id="kg_pipeline", actor_id="critique_and_extract",
            )
            agent_input = {"context_lines": context_block, "indexed_user_lines": indexed_block}
            result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
            data = result.data if result and hasattr(result, "data") else {}
            if not isinstance(data, dict):
                return {"extractable": True, "kept_indices": all_indices,
                        "reason": "critic returned non-dict; defaulting accept"}
            raw_lines = data.get("extractable_lines") or []
            kept: List[int] = []
            for item in raw_lines:
                if not isinstance(item, dict):
                    continue
                idx = item.get("user_message_index")
                if isinstance(idx, int) and 0 <= idx < len(user_lines) and idx not in kept:
                    kept.append(idx)
            kept.sort()
            return {
                "extractable": len(kept) > 0,
                "kept_indices": kept,
                "reason": str(data.get("overall_reason", ""))[:240],
            }
        except Exception as exc:
            logger.warning("[kg_pipeline.critique] window_critic failed (%s: %s); accepting",
                           type(exc).__name__, exc)
            return {"extractable": True, "kept_indices": all_indices,
                    "reason": f"critic error: {type(exc).__name__}"}

    def _call_fact_extractor(self, payload: Dict[str, Any]) -> Tuple[List[Dict], List[Dict]]:
        from app.assistant.ServiceLocator.service_locator import DI

        agent = DI.agent_factory.create_agent(FACT_EXTRACTOR_AGENT_NAME)
        if not agent:
            raise RuntimeError(f"Failed to create agent: {FACT_EXTRACTOR_AGENT_NAME}")
        scope = load_scope_for_source(
            kind="pipeline", source_id="kg_pipeline", actor_id="critique_and_extract",
        )
        agent_input = {"text": payload["user_text"]}
        if payload.get("full_conversation"):
            agent_input["full_conversation"] = payload["full_conversation"]
        if payload.get("message_timestamp"):
            agent_input["original_message_timestamp"] = payload["message_timestamp"]
        result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope))
        data = result.data if result and hasattr(result, "data") else {}
        nodes, edges = normalize_extraction_result(data or {})
        return nodes, edges

    def _persist_extraction(self, window_id: str, verdict: str, reason: str,
                             nodes: Optional[List[Dict]], edges: Optional[List[Dict]]) -> None:
        get_db_manager().write(
            KGWindowExtraction(
                window_id=window_id,
                verdict=verdict,
                verdict_reason=reason[:1000] if reason else None,
                nodes=nodes if nodes is not None else None,
                edges=edges if edges is not None else None,
                extractor_version=EXTRACTOR_VERSION,
                critic_version=CRITIC_VERSION,
            ),
            op="extract.persist_extraction",
        )

    def run_one_cycle(self, ctx) -> int:
        window_ids = self._claim_pending_window_ids(MAX_SEGMENTS_PER_CYCLE)
        if not window_ids:
            return 0

        processed = 0
        for window_id in window_ids:
            payload = self._load_window_lines(window_id)
            if payload is None:
                self._persist_extraction(window_id, "skipped", "no usable user content", None, None)
                processed += 1
                continue

            user_lines = payload["user_lines"]
            if user_lines and all("(unknown)" in line for line in user_lines):
                self._persist_extraction(
                    window_id, "skipped",
                    "all user lines have (unknown) referents", None, None,
                )
                processed += 1
                continue

            verdict = self._call_window_critic(payload["user_lines"], payload["context_lines"])
            if not verdict.get("extractable", True):
                self._persist_extraction(
                    window_id, "rejected",
                    verdict.get("reason", "critic rejected"), None, None,
                )
                processed += 1
                continue

            # v2 line-item filter: only the lines the critic marked extractable
            # become the extraction surface. full_conversation stays unchanged
            # so anaphora/discourse remain coherent for the extractor.
            kept_indices = verdict.get("kept_indices") or list(range(len(payload["user_lines"])))
            kept_user_lines = [payload["user_lines"][i] for i in kept_indices
                               if 0 <= i < len(payload["user_lines"])]
            payload["user_text"] = "\n".join(kept_user_lines)

            try:
                nodes, edges = self._call_fact_extractor(payload)
            except Exception as exc:
                logger.exception("[kg_pipeline.extract] fact_extractor failed for %s", window_id)
                self._persist_extraction(
                    window_id, "error",
                    f"{type(exc).__name__}: {str(exc)[:200]}", None, None,
                )
                processed += 1
                continue

            if not nodes and not edges:
                self._persist_extraction(window_id, "extracted", "empty extraction", [], [])
            else:
                self._persist_extraction(window_id, "extracted", "", nodes, edges)
            processed += 1

        return processed
