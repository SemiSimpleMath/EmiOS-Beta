"""Coalesced process snapshots for the runtime monitor, never liveness authority.

One daemon publishes at most once per second while active, and a five-second
heartbeat while idle. Network/model/tool paths only update the in-memory registry.
"""
from datetime import datetime, timezone
import json
import os
import threading
import psutil

_lock = threading.Lock()
_started = False


def publish_snapshot(directory, registry):
    from app.assistant.utils.atomic_write import write_json_atomic
    payload = registry.snapshot()
    payload.update(pid=os.getpid(), process_started=psutil.Process().create_time(),
                   generated_at_utc=datetime.now(timezone.utc).isoformat())
    path = directory / ('execution_' + payload['process_instance_id'].replace(':', '_') + '.json')
    write_json_atomic(path, payload)


def read_process_snapshots(directory, *, current_process_id):
    """Only fresh reports from the same still-running OS process incarnation."""
    result = []
    for path in directory.glob('execution_*.json'):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            if payload['process_instance_id'] == current_process_id:
                continue
            process = psutil.Process(payload['pid'])
            if process.create_time() != payload['process_started'] or not process.is_running():
                continue
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload['generated_at_utc'])).total_seconds()
            if 0 <= age <= 10:
                result.append(payload)
        except (OSError, ValueError, KeyError, TypeError, psutil.Error):
            continue
    return result


def start_publication(registry):
    global _started
    with _lock:
        if _started:
            return
        _started = True
    def run():
        from app.assistant.routine_manager.utils import status_dir
        from app.assistant.utils.logging_config import get_logger
        logger = get_logger(__name__)
        event = threading.Event()
        while True:
            try:
                publish_snapshot(status_dir(), registry)
            except Exception:
                logger.debug('Execution status publication failed', exc_info=True)
            # Publication heartbeat does not count as execution progress.
            with registry.lock:
                active = bool(registry.active)
            event.wait(1 if active else 5)
    threading.Thread(target=run, name='execution-status-publisher', daemon=True).start()
