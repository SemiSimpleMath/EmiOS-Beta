from __future__ import annotations

from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pipeline_state import get_resume_target, set_resume_target

logger = get_logger(__name__)


class SummaryPostNode(ControlNode):
    """
    Post-summary deterministic router.

    Responsibilities:
    - Validate summary completion path.
    - Route back to configured resume agent (typically planner).
    """

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)
        cfg = self._cfg()

        summary_agent = self._required_str(cfg, "summary_agent")
        default_resume_agent = self._required_str(cfg, "resume_agent")
        resume_target_state_key = self._optional_str(cfg, "resume_target_state_key", "resume_target")
        if resume_target_state_key != "resume_target":
            raise ValueError(
                f"[{self.name}] Unsupported resume_target_state_key={resume_target_state_key!r}. "
                "Only 'resume_target' is supported."
            )
        resume_agent = self._resolve_resume_agent(default_resume_agent)
        last_agent = self.blackboard.get_state_value("last_agent", None)

        if not isinstance(last_agent, str) or last_agent != summary_agent:
            raise ValueError(
                f"[{self.name}] expected last_agent={summary_agent} before summary post route, got: {last_agent!r}"
            )

        self._suppress_summary_agent_result(summary_agent)
        self._apply_summary_actions()
        self.blackboard.update_state_value("next_agent", resume_agent)
        self.blackboard.update_state_value("last_agent", self.name)
        set_resume_target(self.blackboard, None)

    def _apply_summary_actions(self) -> None:
        window_ids = self.blackboard.get_state_value("summary_window_history_ids", [])
        latest_id = self.blackboard.get_state_value("summary_latest_tool_result_history_id", None)
        cfg = self._cfg()
        allow_latest = bool(cfg.get("allow_summarize_latest_tool_result", True))
        protect_n = int(cfg.get("protect_last_n_results", 0) or 0)
        if not isinstance(window_ids, list):
            raise ValueError("summary_window_history_ids must be a list[int].")
        allowed_ids = {int(x) for x in window_ids if isinstance(x, int) and x > 0}
        if not allowed_ids:
            return

        unsummarized_ids = self.blackboard.get_state_value("summary_window_unsummarized_ids", [])
        if not isinstance(unsummarized_ids, list):
            unsummarized_ids = []
        protected_ids: set[int] = set()
        if not allow_latest and isinstance(latest_id, int):
            protected_ids.add(latest_id)
        if protect_n > 0 and unsummarized_ids:
            for hid in reversed(unsummarized_ids):
                if len(protected_ids) >= protect_n:
                    break
                if isinstance(hid, int) and hid > 0:
                    protected_ids.add(hid)

        summary_by_id = self._summary_by_id()

        hide_ids = self._int_list("hide_ids")
        unhide_ids = self._int_list("unhide_ids")
        pin_ids = self._int_list("pin_ids")
        delete_ids = self._int_list("delete_ids")

        by_id = self._messages_by_history_id()

        ops: list[tuple[str, int, str | None]] = []
        for hid, text in summary_by_id.items():
            ops.append(("summarize", hid, text))
        for hid in hide_ids:
            ops.append(("hide", hid, None))
        for hid in unhide_ids:
            ops.append(("unhide", hid, None))
        for hid in pin_ids:
            ops.append(("pin", hid, None))
        for hid in delete_ids:
            ops.append(("delete", hid, None))

        for op, hid, text in ops:
            if hid not in allowed_ids:
                continue
            if hid in protected_ids and op == "summarize":
                continue
            target = by_id.get(hid)
            if target is None:
                continue
            meta = getattr(target, "metadata", None)
            if not isinstance(meta, dict):
                meta = {}
            if op == "summarize":
                if not isinstance(text, str) or not text.strip():
                    continue
                meta["history_summary"] = text.strip()
                meta["history_summarized"] = True
            elif op == "hide":
                meta["history_hidden"] = True
            elif op == "unhide":
                meta["history_hidden"] = False
            elif op == "pin":
                meta["history_pinned"] = True
            elif op == "delete":
                meta["history_deleted"] = True
                meta["history_hidden"] = True
            target.metadata = meta

    def _messages_by_history_id(self) -> dict[int, object]:
        out: dict[int, object] = {}
        for m in self.blackboard.get_messages() or []:
            meta = getattr(m, "metadata", None)
            if not isinstance(meta, dict):
                continue
            hid = meta.get("history_id")
            if isinstance(hid, int) and hid > 0:
                out[hid] = m
        return out

    def _int_list(self, key: str) -> list[int]:
        raw = self.blackboard.get_state_value(key, [])
        if not isinstance(raw, list):
            raise ValueError(f"{key} must be a list[int].")
        out: list[int] = []
        for x in raw:
            if not isinstance(x, int) or x <= 0:
                raise ValueError(f"{key} must contain only positive integers.")
            out.append(x)
        return out

    def _summary_by_id(self) -> dict[int, str]:
        raw = self.blackboard.get_state_value("summary_pairs", [])
        if not isinstance(raw, list):
            raise ValueError("summary_pairs must be a list of {history_id, summary}.")

        out: dict[int, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("summary_pairs entries must be objects.")
            history_id = item.get("history_id")
            summary_text = item.get("summary")
            if not isinstance(summary_text, str) or not summary_text.strip():
                raise ValueError("summary_pairs.summary must be a non-empty string.")
            if isinstance(history_id, int):
                hid = history_id
            elif isinstance(history_id, str):
                s = history_id.strip()
                if not s.isdigit():
                    raise ValueError("summary_pairs.history_id must be a positive integer id.")
                hid = int(s)
            else:
                raise ValueError("summary_pairs.history_id must be int or numeric string.")
            if hid <= 0:
                raise ValueError("summary_pairs.history_id must be a positive integer id.")
            out[hid] = summary_text.strip()
        return out

    def _resolve_resume_agent(self, default_resume_agent: str) -> str:
        resume_target = get_resume_target(self.blackboard)
        if isinstance(resume_target, str) and resume_target.strip():
            return resume_target.strip()
        return default_resume_agent

    def _suppress_summary_agent_result(self, summary_agent: str) -> None:
        """
        Hide summary non-user-facing payloads from planner context/history.
        """
        try:
            msgs = list(self.blackboard.get_messages() or [])
        except Exception:
            logger.debug("[%s] Failed to read messages for summary suppression", self.name, exc_info=True)
            return

        for msg in reversed(msgs):
            if str(getattr(msg, "data_type", "") or "").strip() != "agent_result":
                continue
            if str(getattr(msg, "sender", "") or "").strip() != summary_agent:
                continue
            try:
                existing = getattr(msg, "metadata", None)
                meta = dict(existing) if isinstance(existing, dict) else {}
                meta["context_suppressed"] = True
                meta["context_suppressed_by"] = self.name
                meta["history_hidden"] = True
                meta["summary_non_actionable"] = True
                msg.metadata = meta
            except Exception:
                logger.debug(
                    "[%s] Failed to mark summary result as suppressed (sender=%r)",
                    self.name,
                    summary_agent,
                    exc_info=True,
                )
            return

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
            raise ValueError(f"summary post requires non-empty '{key}'.")
        return raw.strip()

    @staticmethod
    def _optional_str(cfg: dict, key: str, default: str) -> str:
        raw = cfg.get(key, default)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"summary post key '{key}' must be non-empty when provided.")
        return raw.strip()
