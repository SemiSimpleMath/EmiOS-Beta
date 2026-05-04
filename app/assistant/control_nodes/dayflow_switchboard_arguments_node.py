"""Dayflow switchboard arguments node.

Standalone sibling of ChatSwitchboardArgumentsNode and
MasterRoomSwitchboardArgumentsNode. All three extend ``ControlNode``
directly and share only the pure normalization helper from
``_switchboard_arguments_util``. Dayflow does NOT inherit from the chat-side
nodes — they're parallel constructions for different domains.

Responsibilities specific to dayflow:
- Reject direct nest_home_control dispatches (must go through devices_manager).
- Defensively recover delegate_to from task when the LLM forgets to repeat it.
- Parse a JSON string in task_information into a dict before normalization.
- After the shared normalization runs, write the canonical dispatch
  provenance: an ``action_dispatch:UUID`` row + stamp every acted_on item to
  state="dispatched" + inject ``trigger_context`` into the outgoing tool args
  so the dispatched tool/manager has a backlink to its source dayflow item.
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any, Dict, List

from app.assistant.control_nodes._switchboard_arguments_util import (
    normalize_switchboard_args,
)
from app.assistant.control_nodes.control_node import ControlNode
from app.assistant.dayflow_orchestrator.contracts import get_meta
from app.assistant.dayflow_orchestrator.dayflow_item_writer import (
    resolve_short_id,
    write_dayflow_item,
    write_dayflow_items_batch,
)
from app.assistant.dayflow_orchestrator.state_store import (
    DAYFLOW_ROOM_ID,
    get_dayflow_items,
)
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)


class DayflowSwitchboardArgumentsNode(ControlNode):
    """Dayflow-specific switchboard arguments node — sibling of Chat / MasterRoom."""

    def action_handler(self, message):
        self.blackboard.update_state_value("next_agent", None)

        delegate_to = str(self.blackboard.get_state_value("delegate_to", "") or "").strip()
        task = str(self.blackboard.get_state_value("task", "") or "").strip()
        task_information = self.blackboard.get_state_value("task_information", None)

        if delegate_to == "nest_home_control":
            raise ValueError(
                f"[{self.name}] direct nest_home_control dispatch is disabled. "
                "Route thermostat work via devices_manager or one_shot_tool_runner."
            )

        # Defensive normalization: the switchboard LLM sometimes produces
        # task == "create_dayflow_ticket" without repeating it in
        # delegate_to. Inherit delegate_to from task in that case so the
        # dispatch path sees a valid target. Applies to any known tool
        # name, not just ticket dispatch — keeps tickets symmetric with
        # every other tool.
        if not delegate_to and task and self.tool_registry.get_tool(task):
            delegate_to = task
            self.blackboard.update_state_value("delegate_to", delegate_to)

        # If task_information is a JSON string, parse it to dict before
        # normalize_switchboard_args runs. The form declares str but the
        # downstream tool argument construction expects a dict.
        if isinstance(task_information, str) and task_information.strip():
            import json as _json
            try:
                parsed = _json.loads(task_information)
                if isinstance(parsed, dict):
                    self.blackboard.update_state_value("task_information", parsed)
            except (ValueError, TypeError):
                pass  # Non-JSON string, leave as-is for the normalizer.

        # Shared normalization — sets blackboard action / action_input /
        # tool_arguments. Same code path used by Chat and MasterRoom nodes.
        normalize_switchboard_args(self.blackboard, name=self.name)

        action_name = str(self.blackboard.get_state_value("action", "") or "").strip()
        arguments = self.blackboard.get_state_value("action_input", None)
        if not action_name or not isinstance(arguments, dict):
            raise ValueError(
                f"[{self.name}] action/action_input missing after switchboard normalization."
            )

        # Dayflow's own provenance machinery — distinct from the master_room
        # marker write. Creates an action_dispatch:UUID row + stamps every
        # acted_on item to state=dispatched + injects trigger_context into
        # the outgoing tool args.
        self._persist_dispatch_records(action_name=action_name, arguments=arguments)

        self.blackboard.update_state_value("calling_agent", None)
        self.blackboard.update_state_value("next_agent", None)
        self.blackboard.update_state_value("last_agent", self.name)

    def _persist_dispatch_records(
        self,
        *,
        action_name: str,
        arguments: Dict[str, Any],
    ) -> None:
        from app.assistant.utils.time_utils import get_local_timezone

        acted_on_raw = self.blackboard.get_state_value("acted_on_item_ids", [])
        if acted_on_raw is None:
            acted_on_raw = []
        if not isinstance(acted_on_raw, list):
            raise ValueError(
                f"[{self.name}] acted_on_item_ids must be a list for dispatch provenance."
            )
        if not acted_on_raw:
            raise ValueError(
                f"[{self.name}] dispatch requires non-empty acted_on_item_ids."
            )

        resolved_ids = [resolve_short_id(str(v or "").strip()) for v in acted_on_raw]
        resolved_ids = [v for v in resolved_ids if v]
        if not resolved_ids:
            raise ValueError(
                f"[{self.name}] could not resolve acted_on_item_ids for dispatch."
            )

        existing_items = get_dayflow_items()
        meta_by_id: Dict[str, Dict[str, Any]] = {}
        for item in existing_items:
            if not isinstance(item, dict):
                continue
            meta = get_meta(item)
            item_id = str(meta.get("item_id") or item.get("id") or "").strip()
            if item_id:
                meta_by_id[item_id] = meta

        # Inject trigger_context into the outgoing tool arguments so every
        # dispatched tool (ticket, email, anything) gets a backlink to its
        # source dayflow item(s). Without this, tickets land in the DB with
        # trigger_context={} and there's no way to dedup or trace back.
        # Mutates the dict the tool_caller will consume (same reference).
        primary_meta = meta_by_id.get(resolved_ids[0], {})
        outgoing_trigger = dict(arguments.get("trigger_context") or {})
        outgoing_trigger.setdefault("acted_on_item_ids", list(resolved_ids))
        outgoing_trigger.setdefault(
            "task_id",
            str(primary_meta.get("task_id") or primary_meta.get("item_id") or resolved_ids[0]),
        )
        if primary_meta.get("plan_id"):
            outgoing_trigger.setdefault("plan_id", str(primary_meta["plan_id"]))
        arguments["trigger_context"] = outgoing_trigger

        now_utc = datetime.now(timezone.utc)
        created_local = now_utc.astimezone(get_local_timezone()).strftime("%I:%M %p")
        request_id = str(self.blackboard.get_request_id() or "").strip()

        dispatch_messages: List[Message] = []
        dispatch_records: List[Dict[str, Any]] = []
        for acted_id in resolved_ids:
            source_meta = meta_by_id.get(acted_id, {})
            task_summary = str(source_meta.get("summary") or acted_id).strip() or acted_id
            task_id = str(
                source_meta.get("task_id") or source_meta.get("item_id") or acted_id
            ).strip()
            plan_id = str(source_meta.get("plan_id") or "").strip()
            dispatch_id = str(uuid.uuid4())
            dispatch_item_id = f"action_dispatch:{dispatch_id}"

            metadata: Dict[str, Any] = {
                "item_id": dispatch_item_id,
                "source_type": "action_dispatch",
                "event_type": "dispatch",
                "created_at": now_utc.isoformat(),
                "created_at_local": created_local,
                "summary": f"Dispatching {action_name} for {task_summary}",
                "importance": "medium",
                "actionability": "context_only",
                "state": "artifact",
                "state_reason": "action_dispatched",
                "last_reviewed_at": now_utc.isoformat(),
                "dispatch_id": dispatch_id,
                "dispatch_status": "in_flight",
                "request_id": request_id,
                "action_type": action_name,
                "action_arguments": arguments,
                "acted_on_item_id": acted_id,
                "task_id": task_id,
                "plan_id": plan_id,
                "task_summary": task_summary,
            }
            dispatch_messages.append(
                Message(
                    id=dispatch_item_id,
                    data_type="dayflow_input_item",
                    sub_data_type=["dayflow_orchestrator", "action_dispatch", "dayflow_action_log"],
                    sender="dayflow_orchestrator::switchboard",
                    content=str(metadata.get("summary") or ""),
                    timestamp=now_utc,
                    room_id=DAYFLOW_ROOM_ID,
                    metadata=metadata,
                )
            )
            dispatch_records.append({
                "dispatch_id": dispatch_id,
                "dispatch_item_id": dispatch_item_id,
                "acted_on_item_id": acted_id,
                "task_id": task_id,
                "plan_id": plan_id,
                "task_summary": task_summary,
                "action_type": action_name,
            })

        # Stamp dispatched_at and move state to "dispatched" for every acted-on
        # item. The room invocation owns the in-flight window; post_room_finalize
        # will close the item when the tool returns.
        for acted_id in resolved_ids:
            source_meta = meta_by_id.get(acted_id, {})
            updates = {
                "dispatched_at": now_utc.isoformat(),
                "dispatched_at_local": created_local,
            }
            write_dayflow_item(
                acted_id,
                state="dispatched",
                updates=updates,
                reason="action_dispatched",
                caller=self.name,
            )
            source_meta.update(updates)
            source_meta["state"] = "dispatched"
            source_meta["state_reason"] = "action_dispatched"
        stamped_count = len(resolved_ids)

        # Dispatch records are new items — batch write.
        if dispatch_messages:
            batch_items = []
            for msg in dispatch_messages:
                item_dict = dict(msg.metadata)
                item_dict.setdefault("item_id", msg.id)
                item_dict.setdefault("source_type", "action_dispatch")
                item_dict.setdefault("summary", msg.content)
                batch_items.append(item_dict)
            write_dayflow_items_batch(batch_items, caller=self.name)
        self.blackboard.update_state_value("active_dispatch_records", dispatch_records)
        logger.info(
            "[%s] persisted %d pre-dispatch record(s) and stamped %d task item(s) for action=%s.",
            self.name,
            len(dispatch_records),
            stamped_count,
            action_name,
        )
