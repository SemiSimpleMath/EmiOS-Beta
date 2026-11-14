"""The weekly grocery order's own record (subconscious/grocery_lists.py), for the work that places the order.

get: the approved list the owner signed off on /meals, item by item, with the time it is to be ordered.
ordered: the order was placed; the note says what the store confirmed (order number, total, delivery).
delivered: the receipt came; the items it lists (substitutions included) go into the kitchen inventory.
"""
from __future__ import annotations

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult

_ACTIONS = ("get", "ordered", "delivered")


class GroceryOrderTool(BaseTool):
    def __init__(self):
        super().__init__("grocery_order")

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        from app.assistant.subconscious import grocery_lists

        args = (tool_message.tool_data or {}).get("arguments", {})
        action = str(args.get("action") or "").strip()
        if action not in _ACTIONS:
            return make_tool_error(error_code="bad_action", message=f"action must be one of {', '.join(_ACTIONS)}.")
        week = str(args.get("week_start") or "").strip()
        if not week:
            row = grocery_lists.latest(statuses=("approved", "ordered"))
            if row is None:
                return make_tool_error(error_code="no_open_order",
                                       message="No approved or ordered grocery list; nothing to order.")
            week = row["week_start"]
        try:
            if action == "get":
                row = grocery_lists.get(week)
                if row is None:
                    return make_tool_error(error_code="no_list", message=f"No grocery list for the week of {week}.")
            elif action == "ordered":
                row = grocery_lists.mark_ordered(week, note=str(args.get("note") or ""))
            else:
                row = grocery_lists.mark_delivered(week, items=list(args.get("items") or []),
                                                   note=str(args.get("note") or ""))
        except ValueError as e:
            return make_tool_error(error_code="refused", message=str(e))
        return ToolResult(result_type="success", content=_describe(row, action),
                          data={"week_start": row["week_start"], "status": row["status"],
                                "items": row["items"], "order_at": row["order_at"]})


def _describe(row, action: str) -> str:
    from app.assistant.utils.time_utils import local_time_text
    head = f"Grocery list for the week of {row['week_start']}: {row['status']}."
    if action == "ordered":
        return f"{head} Recorded the order: {row['order_note']}"
    if action == "delivered":
        return f"{head} Added to the kitchen inventory: {', '.join(row['delivered_items'])}."
    lines = [head]
    if row["status"] == "approved":
        lines.append(f"Approved by the owner; to be ordered from Ralphs through DoorDash at "
                     f"{local_time_text(row['order_at'])}. Order exactly these items:")
    elif row["status"] != "draft":
        lines.append("Items:")
    else:
        lines.append("Not approved yet; nothing is to be ordered. Items:")
    lines += [f"- {i['name']}" + (f" ({i['quantity']})" if i["quantity"] else "") for i in row["items"]]
    if row["order_note"]:
        lines.append(f"Order placed: {row['order_note']}")
    return "\n".join(lines)


def get_tool_class():
    return GroceryOrderTool
