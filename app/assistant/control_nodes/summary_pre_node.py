from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_resume_target, set_resume_target

logger = get_logger(__name__)


class SummaryPreNode(ControlNode):
    """
    Pre-summary deterministic router.

    Responsibilities:
    - Evaluate whether summary should run.
    - Build and publish a structured summary payload.
    - Route to summary agent or skip directly to resume path.
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        source_agent = self._optional_str(cfg, "source_agent", "tool_return_router")
        summary_agent = self._required_str(cfg, "summary_agent")
        default_resume_agent = self._required_str(cfg, "resume_agent")
        resume_target_state_key = self._optional_str(cfg, "resume_target_state_key", "resume_target")
        if resume_target_state_key != "resume_target":
            raise ValueError(
                f"[{self.name}] Unsupported resume_target_state_key={resume_target_state_key!r}. "
                "Only 'resume_target' is supported."
            )
        resume_agent = self._resolve_resume_agent(default_resume_agent)
        payload_key = self._optional_str(cfg, "payload_key", "summary_payload")
        payload_history_messages = int(cfg.get("payload_recent_history_messages", 120) or 120)
        if payload_history_messages <= 0:
            raise ValueError("payload_recent_history_messages must be > 0")

        last_agent = self.blackboard.get_state_value("last_agent", None)
        if not isinstance(last_agent, str) or last_agent != source_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={source_agent} before summary pre route, got: {last_agent!r}"
            )

        if not bool(cfg.get("enabled", False)):
            self.blackboard.update_state_value("next_agent", resume_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            set_resume_target(self.blackboard, None)
            return

        planner_agent = self._optional_str(cfg, "planner_agent", resume_agent)
        should_run, reason = self._should_run(cfg=cfg, planner_agent=planner_agent)
        if not should_run:
            self.blackboard.update_state_value("next_agent", resume_agent)
            self.blackboard.update_state_value("last_agent", self.name)
            set_resume_target(self.blackboard, None)
            return

        payload = self._build_payload(
            cfg=cfg,
            trigger_reason=reason,
            max_messages=payload_history_messages,
            planner_agent=planner_agent,
        )
        self.blackboard.update_state_value(payload_key, payload)
        logger.debug("[%s] summary triggered reason=%s", self.name, reason)

        self.blackboard.update_state_value("next_agent", summary_agent)
        self.blackboard.update_state_value("last_agent", self.name)

    def _resolve_resume_agent(self, default_resume_agent: str) -> str:
        resume_target = get_resume_target(self.blackboard)
        if isinstance(resume_target, str) and resume_target.strip():
            return resume_target.strip()
        return default_resume_agent

    def _build_payload(self, *, cfg: dict, trigger_reason: str, max_messages: int, planner_agent: str) -> dict:
        scope_id = self.blackboard.get_current_scope_id()
        messages = self.blackboard.get_messages_for_scope(scope_id) if scope_id else []
        messages = self._planner_visible_messages(messages=messages, planner_agent=planner_agent)

        window_rows = self._build_tool_result_window(messages or [])
        window_ids = [int(r["history_id"]) for r in window_rows if isinstance(r, dict) and isinstance(r.get("history_id"), int)]
        unsummarized_ids = [
            int(r["history_id"])
            for r in window_rows
            if isinstance(r, dict)
            and isinstance(r.get("history_id"), int)
            and not bool(r.get("is_summary", False))
        ]
        already_summarized_ids = [
            int(r["history_id"])
            for r in window_rows
            if isinstance(r, dict)
            and isinstance(r.get("history_id"), int)
            and bool(r.get("is_summary", False))
        ]
        latest_tool_result_id = window_ids[-1] if window_ids else None
        self.blackboard.update_state_value("summary_window_history_ids", window_ids)
        self.blackboard.update_state_value("summary_window_unsummarized_ids", unsummarized_ids)
        self.blackboard.update_state_value("summary_window_summarized_ids", already_summarized_ids)
        self.blackboard.update_state_value("summary_latest_tool_result_history_id", latest_tool_result_id)
        self.blackboard.update_state_value(
            "summary_window_history_map",
            [
                {
                    "history_id": int(r["history_id"]),
                    "message_id": r.get("message_id"),
                    "tool_result_id": r.get("tool_result_id"),
                }
                for r in window_rows
                if isinstance(r, dict) and isinstance(r.get("history_id"), int)
            ],
        )

        rendered_lines: list[str] = []
        for row in window_rows:
            hid = row.get("history_id")
            time_s = row.get("time")
            tool_name = row.get("tool_name")
            text = row.get("display_text")
            rendered_lines.append(f"[{hid}] {time_s} - {tool_name}")
            rendered_lines.append(f"Content: {text}")
        history = "\n".join(rendered_lines).strip()
        return {
            "trigger_reason": trigger_reason,
            "recent_history": history,  # Text view for the summary agent.
            "window_entries": window_rows,  # Structured entries keyed by stable history_id.
            "unsummarized_ids": unsummarized_ids,
            "already_summarized_ids": already_summarized_ids,
            "latest_tool_result_history_id": latest_tool_result_id,
            "task": self.blackboard.get_state_value("task", ""),
            "information": self.blackboard.get_state_value("information", ""),
            "checklist": self.blackboard.get_state_value("checklist", []),
            "progress": self.blackboard.get_state_value("progress", []),
        }

    def _should_run(self, *, cfg: dict, planner_agent: str) -> tuple[bool, str]:
        large_threshold = int(cfg.get("trigger_on_large_result_chars", 1000) or 1000)
        if large_threshold <= 0:
            raise ValueError("trigger_on_large_result_chars must be > 0")
        latest_size = self._latest_tool_result_chars(planner_agent=planner_agent)
        if latest_size >= large_threshold:
            return True, "large_result"

        try:
            action_key_t = self._optional_str(cfg, "action_count_state_key", "{planner_agent}_action_count")
            action_key = action_key_t.format(planner_agent=planner_agent)
            steps = int(self.blackboard.get_state_value(action_key, 0) or 0)
        except Exception as e:
            logger.error("[%s] Failed reading action count for summary gate: %s", self.name, e)
            logger.debug("[%s] summary action count exception details", self.name, exc_info=True)
            raise

        if steps <= 0:
            return False, "no_steps"

        cadence = int(cfg.get("cadence_every_actions", 4) or 4)
        if cadence <= 0:
            raise ValueError("cadence_every_actions must be > 0")
        if (steps % cadence) != 0:
            return False, "cadence_skip"

        min_messages = int(cfg.get("min_messages", 140) or 140)
        if min_messages <= 0:
            raise ValueError("min_messages must be > 0")
        msg_count = len(
            self._planner_visible_messages(
                messages=self.blackboard.get_messages() or [],
                planner_agent=planner_agent,
            )
        )
        if msg_count < min_messages:
            return False, "message_threshold_skip"
        return True, "cadence"

    def _latest_tool_result_chars(self, *, planner_agent: str) -> int:
        messages = self._planner_visible_messages(
            messages=self.blackboard.get_messages() or [],
            planner_agent=planner_agent,
        )
        for m in reversed(messages):
            if str(getattr(m, "data_type", "") or "").strip() != "tool_result":
                continue
            meta = getattr(m, "metadata", None)
            if isinstance(meta, dict) and bool(meta.get("history_deleted", False)):
                continue
            content = str(getattr(m, "content", "") or "")
            return len(content)
        return 0

    @staticmethod
    def _planner_visible_messages(*, messages: list, planner_agent: str) -> list:
        if not isinstance(messages, list):
            return []
        if not isinstance(planner_agent, str) or not planner_agent.strip():
            raise ValueError("planner_agent must be a non-empty string.")
        planner = planner_agent.strip()
        out = []
        for m in messages:
            sender = getattr(m, "sender", None)
            receiver = getattr(m, "receiver", None)
            sender_ok = isinstance(sender, str) and sender.strip() == planner
            receiver_ok = isinstance(receiver, str) and receiver.strip() == planner
            if sender_ok or receiver_ok:
                out.append(m)
        return out

    @staticmethod
    def _build_tool_result_window(messages: list) -> list[dict]:
        rows: list[dict] = []
        for m in messages:
            if str(getattr(m, "data_type", "") or "").strip() != "tool_result":
                continue
            meta = getattr(m, "metadata", None)
            if not isinstance(meta, dict):
                continue
            history_id = meta.get("history_id")
            if not isinstance(history_id, int) or history_id <= 0:
                continue
            if bool(meta.get("history_deleted", False)):
                continue
            summary_text = str(meta.get("history_summary", "") or "").strip()
            hidden = bool(meta.get("history_hidden", False))
            pinned = bool(meta.get("history_pinned", False))
            if hidden and not summary_text:
                continue
            sub = getattr(m, "sub_data_type", None)
            tool_name = "tool"
            if isinstance(sub, list) and len(sub) > 0 and isinstance(sub[0], str) and sub[0].strip():
                tool_name = sub[0].strip()
            ts = getattr(m, "timestamp", None)
            time_s = ""
            if isinstance(ts, str):
                # Keep cheap formatting in summary payload.
                time_s = ts[11:16] if len(ts) >= 16 else ts
            content = str(getattr(m, "content", "") or "").strip()
            display_text = summary_text if summary_text else content
            tool_result_id = meta.get("tool_result_id") if isinstance(meta.get("tool_result_id"), str) else None
            message_id = getattr(m, "id", None)
            message_id = message_id if isinstance(message_id, str) and message_id.strip() else None
            rows.append(
                {
                    "history_id": history_id,
                    "message_id": message_id,
                    "tool_result_id": tool_result_id,
                    "time": time_s,
                    "tool_name": tool_name,
                    "display_text": display_text,
                    "is_summary": bool(summary_text),
                    "hidden": hidden,
                    "pinned": pinned,
                }
            )
        return rows

    def _cfg(self) -> dict:
        flow_cfg = self.blackboard.get_state_value("manager_flow_config", None)
        if not isinstance(flow_cfg, dict):
            raise ValueError("manager_flow_config must be a dict.")
        legacy_cfg = flow_cfg.get("summary")
        if legacy_cfg is None:
            legacy_cfg = {}
        if not isinstance(legacy_cfg, dict):
            raise ValueError("manager_flow_config.summary must be a dict when provided.")

        node_cfg_map = self.blackboard.get_state_value("manager_control_node_configs", {})
        if node_cfg_map is None:
            node_cfg_map = {}
        if not isinstance(node_cfg_map, dict):
            raise ValueError("manager_control_node_configs must be a dict when provided.")
        node_cfg = node_cfg_map.get(self.name, {})
        if node_cfg is None:
            node_cfg = {}
        if not isinstance(node_cfg, dict):
            raise ValueError(f"manager_control_node_configs['{self.name}'] must be a dict when provided.")

        cfg = dict(legacy_cfg)
        cfg.update(node_cfg)
        if not cfg:
            raise ValueError(
                f"[{self.name}] missing summary config. Provide flow_config.summary or control_nodes[{self.name}].config."
            )
        return cfg

    @staticmethod
    def _required_str(cfg: dict, key: str) -> str:
        raw = cfg.get(key)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"summary pre requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"summary pre key '{key}' must be non-empty when provided.")
        return raw.strip()
