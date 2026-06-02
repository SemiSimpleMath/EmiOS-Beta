"""message_manager — writes message/event rows to the unified log.

Synchronous persistence layer for unified_log_2026. Called inline at ingestion
points (chat route, room_session_manager, email, tool results, control-node
markers); NOT scheduled. Moved out of maintenance_manager — the old idle-tick
maintenance subsystem that periodically flushed the blackboard here was removed,
so this writer no longer has any reason to live in a "maintenance" package.
"""
