from __future__ import annotations

import json
from typing import Any, Dict, List
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class FinalAnswerNormalizer:
    @staticmethod
    def to_string(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            try:
                from app.assistant.utils.prompt_formatter import format_for_prompt
                return format_for_prompt(value)
            except Exception:
                pass
        return str(value)

    @staticmethod
    def to_source_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            out: List[str] = []
            for item in value:
                s = str(item).strip()
                if s:
                    out.append(s)
            return out
        if isinstance(value, str):
            raw = value.replace("\r", "\n")
            parts: List[str] = []
            for line in raw.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if "," in line and not line.startswith("http"):
                    parts.extend([x.strip() for x in line.split(",") if x.strip()])
                else:
                    parts.append(line)
            return parts
        s = str(value).strip()
        return [s] if s else []

    @classmethod
    def to_data_list(cls, result_dict: Dict[str, Any]) -> List[Dict[str, str]]:
        v = result_dict.get("final_answer_data_list")
        if isinstance(v, list):
            return v

        skip_keys = {
            "final_answer_task",
            "final_answer_answer",
            "final_answer_no_op",
            "final_answer_what_was_done",
            "final_answer_interesting_info",
            "final_answer_sources",
            "final_answer_sources_used",
            "final_answer_detail_level",
            "final_answer_data_list",
            "task",
            "answer",
            "no_op_tf",
            "what_was_done",
            "interesting_info",
            "sources",
            "sources_used",
            "summary",
            "narrative",
            "status",
            "action",
            "action_input",
            "result",
            "exit",
            "pod_references",
            "result_summary",
            "final_answer_raw",
        }
        lifted: List[Dict[str, str]] = []
        for key, value in result_dict.items():
            if key in skip_keys:
                continue
            lifted.append(
                {
                    "data_type": "detail",
                    "key": str(key),
                    "value": cls.to_string(value),
                }
            )
        return lifted

    @classmethod
    def normalize(cls, result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            result_dict = result
        else:
            result_dict = {"final_answer_answer": cls.to_string(result)}

        # Important contract:
        # If final_answer_answer is explicitly present (including empty string for no-op),
        # preserve it and do not fallback to serializing the whole payload.
        if "final_answer_answer" in result_dict:
            answer = result_dict.get("final_answer_answer")
        else:
            # Only a recognized answer field becomes the answer. A dict with none
            # of them yields an empty answer (its other keys still surface via
            # data_list) instead of serializing the whole payload as the reply —
            # arbitrary internal fields must not reach the user (cf. RSM2).
            answer = (
                result_dict.get("answer")
                or result_dict.get("summary")
                or result_dict.get("narrative")
                or ""
            )
        task = result_dict.get("final_answer_task") or result_dict.get("task") or ""
        no_op = bool(result_dict.get("final_answer_no_op", False) or result_dict.get("no_op_tf", False))
        what_done = (
            result_dict.get("final_answer_what_was_done")
            or result_dict.get("what_was_done")
            or result_dict.get("methods")
            or ""
        )
        interesting = (
            result_dict.get("final_answer_interesting_info")
            or result_dict.get("interesting_info")
            or ""
        )
        sources = cls.to_source_list(
            result_dict.get("final_answer_sources", None)
            if result_dict.get("final_answer_sources", None) is not None
            else result_dict.get("final_answer_sources_used", None)
            if result_dict.get("final_answer_sources_used", None) is not None
            else result_dict.get("sources", None)
            if result_dict.get("sources", None) is not None
            else result_dict.get("sources_used", None)
        )
        data_list = cls.to_data_list(result_dict)

        detail_level = result_dict.get("final_answer_detail_level")
        if not isinstance(detail_level, str) or detail_level not in {"brief", "standard", "full"}:
            if data_list:
                detail_level = "full"
            elif what_done or interesting or sources:
                detail_level = "standard"
            else:
                detail_level = "brief"

        return {
            "final_answer_task": cls.to_string(task),
            "final_answer_answer": cls.to_string(answer),
            "final_answer_no_op": no_op,
            "final_answer_what_was_done": cls.to_string(what_done),
            "final_answer_interesting_info": cls.to_string(interesting),
            "final_answer_sources": sources,
            "final_answer_detail_level": detail_level,
            "final_answer_data_list": data_list,
            # Carry the research-pod fields through the canonical envelope so they reach the human
            # (response_formatter) and dayflow (action_result) instead of being dropped at exit.
            "result_summary": cls.to_string(result_dict.get("result_summary") or ""),
            "pod_references": cls.to_pod_references(result_dict.get("pod_references")),
        }

    @staticmethod
    def to_pod_references(value: Any) -> list:
        """Normalize pod_references to a list of {pod_id, one_liner} dicts (accepts dicts or
        pydantic PodReference objects). Drops entries without a pod_id."""
        out = []
        for r in (value or []):
            if isinstance(r, dict):
                pid = str(r.get("pod_id") or "").strip()
                one = str(r.get("one_liner") or "").strip()
            else:
                pid = str(getattr(r, "pod_id", "") or "").strip()
                one = str(getattr(r, "one_liner", "") or "").strip()
            if pid:
                out.append({"pod_id": pid, "one_liner": one})
        return out

    @classmethod
    def merge_pod_references(cls, *values: Any) -> list:
        """Union pod_references from several sources (a final-answer form field, the accumulated
        relay, a result payload), normalized to {pod_id, one_liner} and deduped by pod_id with
        first-seen order preserved. So a relayed sub-manager pod can't be lost when a final-answer
        agent also emits its own (possibly empty) pod_references list."""
        out: list = []
        seen: set = set()
        for value in values:
            for ref in cls.to_pod_references(value):
                if ref["pod_id"] not in seen:
                    seen.add(ref["pod_id"])
                    out.append(ref)
        return out

    @classmethod
    def attach_carry_through(cls, payload: Any, blackboard: Any) -> Any:
        """Attach the carry-through fields onto the winning final-answer payload before normalize:
        result_summary, and the UNION of pod_references from (the payload's own refs, a final-answer
        form field, the accumulated relay). Keeping these off the has_state_final decision and merging
        the relay here is what prevents both the dropped-answer and the clobbered-relay bugs."""
        if not isinstance(payload, dict):
            return payload
        payload = dict(payload)
        rs = blackboard.get_state_value("result_summary")
        if rs and not payload.get("result_summary"):
            payload["result_summary"] = rs
        pods = cls.merge_pod_references(
            payload.get("pod_references"),
            blackboard.get_state_value("pod_references"),
            blackboard.get_state_value("accumulated_pod_references"),
            # The run's MINTED pods (research_notebook, written by
            # Planner._mint_research_findings) are the ground truth: canonical ids
            # recorded at mint time, immune to final-answer transcription.
            blackboard.get_state_value("research_notebook"),
        )
        if pods:
            # pod_id canonical or invisible: an id that is not a well-formed pod URI
            # is an LLM transcription (a slugified title like "msa-september-30-2026")
            # — it can never be fetched, attached, or linked. Drop it rather than
            # propagate a dead reference into the graph and the ticket link path.
            from app.assistant.pod_store.pod_uri import POD_URI_RE
            valid = [p for p in pods if POD_URI_RE.fullmatch(p["pod_id"])]
            if len(valid) != len(pods):
                logger.warning(
                    "attach_carry_through: dropped %d non-canonical pod ref(s): %s",
                    len(pods) - len(valid),
                    [p["pod_id"] for p in pods if not POD_URI_RE.fullmatch(p["pod_id"])],
                )
            payload["pod_references"] = valid
        return payload
