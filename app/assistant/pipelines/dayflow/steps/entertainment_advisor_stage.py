"""
Entertainment Advisor Stage

Daily life-domain advisor that proactively suggests entertainment, leisure,
dining, and hobby activities when the timing is right. Runs at a low cadence
(~once per hour) and is designed to stay silent most of the time.

Output is written to resource_entertainment_advisor_output.json. When a
suggestion has requires_action=true, it is forwarded as a dayflow item
for the orchestrator's planner to pick up.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.assistant.pipelines.dayflow.step_types import BaseStep, StepContext, StepResult
from app.assistant.pipelines.scope_policy import build_pipeline_scope_context
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir as _get_resources_dir
from app.assistant.utils.time_utils import utc_to_local

logger = get_logger(__name__)

_OUTPUT_FILENAME = "resource_entertainment_advisor_output.json"
_BELIEFS_PATH = _get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"
_BIO_PATH = _get_resources_dir() / "user_bio.json"
_ENTERTAINMENT_TAGS = frozenset({"entertainment", "hobbies", "music", "food", "social", "dining"})


class EntertainmentAdvisorStep(BaseStep):
    step_id: str = "entertainment_advisor"

    def _build_pipeline_scope_context(self, agent_input: Dict[str, Any]):
        return build_pipeline_scope_context(
            pipeline_id="dayflow",
            actor_id=f"{self.step_id}_runner",
            room_id=None,
            room_surface=None,
        )

    def _get_afk_snapshot(self) -> Dict[str, Any]:
        from app.assistant.ServiceLocator.service_locator import DI

        monitor = getattr(DI, "afk_monitor", None)
        if monitor is None:
            logger.error("EntertainmentAdvisorStep: DI.afk_monitor is missing")
            raise RuntimeError("AFKMonitor missing: DI.afk_monitor is None")
        snapshot = monitor.get_computer_activity()
        if not isinstance(snapshot, dict):
            logger.error("EntertainmentAdvisorStep: AFKMonitor returned invalid type=%s", type(snapshot))
            raise RuntimeError(f"AFKMonitor returned invalid type: {type(snapshot)}")
        return snapshot

    def _get_last_run_utc(self, ctx: StepContext) -> Optional[datetime]:
        step_runs = ctx.state.get("step_runs", {})
        info = step_runs.get(self.step_id, {}) if isinstance(step_runs, dict) else {}
        raw = info.get("last_run_utc") if isinstance(info, dict) else None
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception as e:
            logger.debug("EntertainmentAdvisorStep: could not parse last_run_utc %r: %s", raw, e, exc_info=True)
            return None

    def should_run(self, ctx: StepContext) -> Tuple[bool, str]:
        stage_cfg = ctx.step_config or {}
        run_policy = stage_cfg.get("run_policy", {}) if isinstance(stage_cfg, dict) else {}
        min_interval = int(run_policy.get("min_interval_seconds", 3600))

        last_run_utc = self._get_last_run_utc(ctx)
        if last_run_utc:
            elapsed = (ctx.now_utc - last_run_utc).total_seconds()
            if elapsed < min_interval:
                return False, f"interval={int(min_interval - elapsed)}s remaining"

        afk_guard = stage_cfg.get("afk_guard", {}) if isinstance(stage_cfg, dict) else {}
        if isinstance(afk_guard, dict) and afk_guard.get("skip_when_afk", True):
            snapshot = self._get_afk_snapshot()
            if bool(snapshot.get("is_afk", False)):
                return False, "afk_guard=afk"

        return True, "ready"

    def _load_entertainment_beliefs(self) -> List[str]:
        """Extract entertainment-related beliefs from the beliefs resource."""
        try:
            if not _BELIEFS_PATH.exists():
                return []
            raw = json.loads(_BELIEFS_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return []
            beliefs_list = raw.get("beliefs", [])
            if not isinstance(beliefs_list, list):
                return []
            results: List[str] = []
            for belief in beliefs_list:
                if not isinstance(belief, dict):
                    continue
                tags = belief.get("tags", [])
                if not isinstance(tags, list):
                    continue
                if any(str(t).strip().lower() in _ENTERTAINMENT_TAGS for t in tags):
                    text = str(belief.get("belief", "")).strip()
                    if text:
                        results.append(text)
            return results
        except Exception as e:
            logger.debug("EntertainmentAdvisorStep: failed loading beliefs: %s", e, exc_info=True)
            return []

    def _load_entertainment_bio(self) -> str:
        """Load the entertainment_hobbies section from user bio."""
        try:
            if not _BIO_PATH.exists():
                return ""
            raw = json.loads(_BIO_PATH.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return ""
            sections = raw.get("sections", raw)
            if isinstance(sections, dict):
                return str(sections.get("entertainment_hobbies", "")).strip()
            return ""
        except Exception as e:
            logger.debug("EntertainmentAdvisorStep: failed loading bio: %s", e, exc_info=True)
            return ""

    def _load_previous_assessment(self, ctx: StepContext) -> str:
        """Load previous output for continuity."""
        prev = ctx.read_resource(_OUTPUT_FILENAME)
        if not isinstance(prev, dict):
            return ""
        assessment = str(prev.get("assessment", "")).strip()
        if not assessment:
            return ""
        updated = str(prev.get("last_updated", "")).strip()
        return f"Previous assessment ({updated}): {assessment}"

    def _get_location_summary(self) -> str:
        try:
            from app.assistant.location_manager.location_manager import get_location_manager
            current = get_location_manager().get_current_location()
            return f"Currently: {current.get('label', 'Unknown')}"
        except Exception as e:
            logger.debug("EntertainmentAdvisorStep: could not get location: %s", e, exc_info=True)
            return ""

    def _call_agent(self, agent_input: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            from app.assistant.ServiceLocator.service_locator import DI
            from app.assistant.utils.pydantic_classes import Message

            agent = DI.agent_factory.create_agent("entertainment_advisor")
            scope_context = self._build_pipeline_scope_context(agent_input)
            result = agent.action_handler(Message(agent_input=agent_input, scope_context=scope_context))

            if hasattr(result, "data") and isinstance(result.data, dict):
                return result.data
            elif isinstance(result, dict):
                return result
            else:
                return {"result": str(result)}
        except Exception as e:
            logger.error("EntertainmentAdvisorStep: agent call failed: %s", e)
            logger.debug("EntertainmentAdvisorStep agent call exception details", exc_info=True)
            raise

    def _forward_actionable_suggestions(
        self,
        suggestions: List[Dict[str, Any]],
        now_utc: datetime,
    ) -> int:
        """Forward suggestions with requires_action=true as dayflow items."""
        import hashlib
        from app.assistant.utils.pydantic_classes import Message

        actionable = [s for s in suggestions if isinstance(s, dict) and s.get("requires_action")]
        if not actionable:
            return 0

        messages: List[Message] = []
        for suggestion in actionable:
            title = str(suggestion.get("title", "")).strip()
            if not title:
                continue

            # Seed by LOCAL date — a suggestion at 11pm PT and the same
            # suggestion at 9am PT next morning are different days for the
            # user, but both fall in the next UTC date.
            date_seed = utc_to_local(now_utc).strftime("%Y-%m-%d")
            item_id = f"ent:{hashlib.sha256(f'{title}|{date_seed}'.encode()).hexdigest()[:16]}"

            metadata: Dict[str, Any] = {
                "item_id": item_id,
                "source_type": "entertainment_advisor",
                "event_type": "entertainment_suggestion",
                "created_at": now_utc.isoformat(),
                "summary": title,
                "importance": "low",
                "actionability": "actionable",
                "state": "new",
                "state_reason": "entertainment_advisor_suggestion",
                "last_reviewed_at": now_utc.isoformat(),
                "needs_planning": True,
                "category": str(suggestion.get("category", "")).strip(),
                "reasoning": str(suggestion.get("reasoning", "")).strip(),
                "action_description": str(suggestion.get("action_description", "")).strip(),
                "effort_level": str(suggestion.get("effort_level", "low")).strip(),
                "cooldown_until": None,
                "linked_item_ids": [],
            }

            msg = Message(
                id=item_id,
                data_type="dayflow_input_item",
                sub_data_type=["dayflow_orchestrator", "entertainment_suggestion"],
                sender="entertainment_advisor",
                content=title,
                timestamp=now_utc,
                room_id="dayflow_orchestrator",
                metadata=metadata,
            )
            messages.append(msg)

        if messages:
            from app.assistant.dayflow_orchestrator.dayflow_item_writer import write_dayflow_items_batch
            batch = []
            for msg in messages:
                meta = dict(msg.metadata) if isinstance(msg.metadata, dict) else {}
                meta.setdefault("item_id", msg.id)
                meta.setdefault("summary", msg.content or "")
                meta.setdefault("source_type", meta.get("source_type", "entertainment_advisor"))
                batch.append(meta)
            write_dayflow_items_batch(batch, caller="entertainment_advisor")
            logger.info(
                "EntertainmentAdvisorStep: forwarded %d actionable suggestion(s) as dayflow items.",
                len(messages),
            )

        return len(messages)

    def run(self, ctx: StepContext) -> StepResult:
        context: Dict[str, Any] = {
            "day_of_week": ctx.now_local.strftime("%A"),
            "location_summary": self._get_location_summary(),
            "entertainment_beliefs": self._load_entertainment_beliefs(),
            "entertainment_bio": self._load_entertainment_bio(),
            "previous_assessment": self._load_previous_assessment(ctx),
            "recent_entertainment_history": [],
        }

        output = self._call_agent(context)
        if not output:
            logger.warning("EntertainmentAdvisorStep: agent returned no output")
            return StepResult(output={"error": "agent returned no output"}, debug={})

        suggestions = output.get("suggestions", [])
        if not isinstance(suggestions, list):
            suggestions = []

        forwarded = 0
        if output.get("no_action"):
            logger.info(
                "EntertainmentAdvisorStep: no_action=true, reason=%r",
                output.get("no_action_reason", ""),
            )
        elif suggestions:
            forwarded = self._forward_actionable_suggestions(suggestions, ctx.now_utc)

        stage_output: Dict[str, Any] = {
            **output,
            "last_updated": ctx.now_local.strftime("%Y-%m-%d %I:%M %p"),
            "forwarded_to_dayflow": forwarded,
        }
        ctx.write_resource(_OUTPUT_FILENAME, stage_output)

        return StepResult(
            output=stage_output,
            debug={
                "no_action": output.get("no_action", False),
                "suggestion_count": len(suggestions),
                "forwarded": forwarded,
            },
        )
