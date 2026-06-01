"""End-to-end test harness for room → manager → outbound flows.

Lets us simulate a real inbound message at a room (e.g., a slack channel)
and watch it flow through the full pipeline — switchboard, manager,
tool calls — to an outbound response, without touching real databases
or sending real messages.

Entry point: ``simulate_room_inbound`` in ``harness``.
"""
