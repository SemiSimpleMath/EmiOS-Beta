"""
Dayflow-specific tool caller node.

Inherits from RoomToolCaller — same execute-and-route-forward behavior.
Exists as a named subclass so the dayflow manager can register it
separately in its control_nodes list.
"""
from app.assistant.control_nodes.room_tool_caller import RoomToolCaller


class DayflowToolCaller(RoomToolCaller):
    pass
