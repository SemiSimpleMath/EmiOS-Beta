import queue
import threading
from collections import deque
from dataclasses import dataclass
from time import time
from typing import Callable, Deque, Dict, List, Optional, Sequence

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message
from app.assistant.runtime import MonitoredThreadPoolExecutor, start_monitored_thread

logger = get_logger(__name__)

# Topics that are legitimately published with no subscriber (optional
# side-channels) — the no-subscriber warning is suppressed for these so it
# stays a reliable "you published into the void" signal for everything else.
_NO_SUBSCRIBER_OK: frozenset[str] = frozenset({"settings_changed"})


@dataclass(frozen=True)
class _MailboxItem:
    ts: float
    message: Message


class EventHub:
    """
    Production-grade event hub:
    - Non-blocking for publishers (publish just enqueues).
    - No thread-per-message. Delivery runs via a bounded worker pool.
    - "Busy receiver" semantics are supported via per-receiver mailboxes:
      if receiver is BUSY, messages are buffered until it becomes AVAILABLE.

    Notes:
    - The hub does NOT validate that receivers are agents. Receivers may be agents, managers, or services.
    - Handlers should be fast; heavy work should be delegated to their own queues/threads.
    """

    def __init__(
        self,
        *,
        worker_threads: int = 8,
        max_mailbox_messages_per_receiver: int = 5000,
        mailbox_ttl_seconds: Optional[float] = None,
        drain_per_available_edge: int = 256,
        max_queue_size: int = 5000,
    ):
        # Bounded dispatch queue. If publishers outpace the dispatcher (burst
        # of events during startup, stuck handler, misbehaving producer),
        # publish() blocks briefly rather than letting the queue grow the heap
        # without bound. A Full exception becomes the operator signal.
        self.queue: "queue.Queue[Optional[Message]]" = queue.Queue(
            maxsize=max(100, int(max_queue_size)),
        )
        self._stop_event = threading.Event()

        # Event routing table (maps event keys to handler functions)
        self.event_registry: Dict[str, List[Callable[[Message], None]]] = {}

        # Receiver status + mailbox buffering (works for agents, managers, services)
        self.receiver_status: Dict[str, bool] = {}
        self.pending_messages: Dict[str, Deque[_MailboxItem]] = {}
        self.max_mailbox_messages_per_receiver = max(1, int(max_mailbox_messages_per_receiver))
        self.mailbox_ttl_seconds = mailbox_ttl_seconds
        self.drain_per_available_edge = max(1, int(drain_per_available_edge))

        # Protect event registry + statuses + mailboxes (hub is multi-threaded)
        self._lock = threading.Lock()

        # Fixed worker pool for handler execution (no thread-per-message)
        self._executor = MonitoredThreadPoolExecutor(
            name="event-handler-hub",
            owner="event_hub",
            max_workers=max(1, int(worker_threads)),
            metadata={"component": "event_hub"},
        )

        # Start a single dispatcher thread for routing messages to handlers
        self.worker_thread = start_monitored_thread(
            owner="event_hub",
            name="event-handler-hub-dispatcher",
            target=self.process_queue,
            daemon=True,
            kind="dispatcher_loop",
            metadata={"component": "event_hub", "role": "dispatcher"},
        )
        logger.info("✅ EventHub initialized and dispatcher thread started.")

    def register_event(self, event_key: str, handler: Callable[[Message], None]) -> None:
        """Registers a subscriber for an event. Raises on exact-duplicate handler."""
        with self._lock:
            handlers = self.event_registry.setdefault(event_key, [])
            if any(h == handler for h in handlers):
                raise RuntimeError(
                    f"Duplicate registration for event '{event_key}' with same handler: {handler} (id: {id(handler)})"
                )
            handlers.append(handler)
        logger.info(f"✅ Registered event: {event_key} with handler {handler}")

    def unregister_event(self, event_key: str, handler: Optional[Callable[[Message], None]] = None) -> None:
        """
        Unregister previously registered event subscribers.
        - If `handler` is None: removes ALL subscribers for the event.
        - If `handler` is provided: removes that subscriber only (if present).
        """
        with self._lock:
            if event_key not in self.event_registry:
                logger.warning(f"⚠️ Attempted to unregister unknown event: {event_key}")
                return

            if handler is None:
                removed = self.event_registry.pop(event_key)
                logger.info(f"🗑️ Unregistered event: {event_key} (Subscribers removed: {len(removed)})")
                return

            handlers = self.event_registry.get(event_key, [])
            before = len(handlers)
            handlers = [h for h in handlers if h != handler]
            after = len(handlers)
            if after == before:
                logger.warning(f"⚠️ Handler not found while unregistering event '{event_key}': {handler}")
                return
            if handlers:
                self.event_registry[event_key] = handlers
            else:
                self.event_registry.pop(event_key, None)
            logger.info(f"🗑️ Unregistered subscriber for event: {event_key} (remaining: {after})")

    # Back-compat name (used by Agent.py)
    def set_agent_status(self, agent_name: str, is_busy: bool) -> None:
        self.set_receiver_status(agent_name, is_busy)

    def set_receiver_status(self, receiver: str, is_busy: bool) -> None:
        """Marks a receiver as busy or available. When available, drains buffered mailbox messages."""
        with self._lock:
            previous_status = self.receiver_status.get(receiver, None)
            self.receiver_status[receiver] = bool(is_busy)
        status = "BUSY" if is_busy else "AVAILABLE"
        logger.debug(f"🔄 Receiver '{receiver}' changed from {previous_status} to {status}")

        if is_busy:
            return

        # Drain a bounded number of pending messages directly (avoid flooding global queue).
        drained: List[Message] = []
        with self._lock:
            mailbox = self.pending_messages.get(receiver)
            if not mailbox:
                return

            # Optional TTL cleanup (prevents unbounded growth if a receiver never returns)
            if self.mailbox_ttl_seconds is not None:
                cutoff = time() - float(self.mailbox_ttl_seconds)
                while mailbox and mailbox[0].ts < cutoff:
                    mailbox.popleft()

            max_drain = min(self.drain_per_available_edge, len(mailbox))
            for _ in range(max_drain):
                if not mailbox:
                    break
                drained.append(mailbox.popleft().message)

            if not mailbox:
                self.pending_messages.pop(receiver, None)

        for m in drained:
            self._deliver(m)

    def is_agent_busy(self, agent_name: str) -> bool:
        """Back-compat helper for agent busy status."""
        return self.is_receiver_busy(agent_name)

    def is_receiver_busy(self, receiver: Optional[str]) -> bool:
        if receiver is None:
            return False
        with self._lock:
            return bool(self.receiver_status.get(receiver, False))

    def publish(self, message: Message) -> None:
        """Non-blocking publish: enqueue and return immediately."""
        logger.debug(
            f"🔵 Received event: {message.event_topic} receiver='{message.receiver}' sender='{message.sender}'"
        )

        with self._lock:
            has_subscriber = bool(self.event_registry.get(message.event_topic or "", []))
        if not has_subscriber:
            # Some events are optional and don't require handlers
            if message.event_topic in _NO_SUBSCRIBER_OK:
                logger.debug(f"ℹ️ Event '{message.event_topic}' published (no subscribers yet; normal)")
            else:
                logger.warning(f"⚠️ Event '{message.event_topic}' was published but has NO registered subscribers yet.")

        # Bounded put with a short timeout — if the dispatcher is wedged for
        # more than a few seconds, raise loudly so the caller knows its event
        # was dropped, rather than blocking forever or growing the heap.
        try:
            self.queue.put(message, timeout=5.0)
        except queue.Full:
            logger.error(
                "EventHub queue full (size=%d/%d) — dropping message topic=%r sender=%r. "
                "Dispatcher may be stuck or event rate exceeds worker pool capacity.",
                self.queue.qsize(), self.queue.maxsize,
                getattr(message, "event_topic", None),
                getattr(message, "sender", None),
            )

    def _deliver(self, message: Message) -> None:
        receiver = message.receiver

        # If the receiver is busy, buffer it in that receiver mailbox (FIFO)
        if receiver is not None:
            with self._lock:
                if bool(self.receiver_status.get(receiver, False)):
                    mailbox = self.pending_messages.setdefault(receiver, deque())
                    if len(mailbox) >= self.max_mailbox_messages_per_receiver:
                        # Drop oldest to preserve more recent state transitions.
                        mailbox.popleft()
                        logger.warning(
                            f"⚠️ Mailbox overflow for receiver='{receiver}'. Dropped oldest message. "
                            f"max={self.max_mailbox_messages_per_receiver}"
                        )
                    mailbox.append(_MailboxItem(ts=time(), message=message))
                    return

        handlers: Sequence[Callable[[Message], None]]
        with self._lock:
            handlers = tuple(self.event_registry.get(message.event_topic or "", []))

        if not handlers:
            handlers = (self.default_handler,)

        # Fan-out to all subscribers (bounded worker pool).
        for handler in handlers:
            self._executor.submit(self._safe_invoke, handler, message)

    def _safe_invoke(self, handler: Callable[[Message], None], message: Message) -> None:
        try:
            handler(message)
        except Exception:
            # Never crash the hub for a handler bug; log and continue.
            logger.critical(
                f"🔥 Handler crashed for event '{message.event_topic}' receiver='{message.receiver}' handler={handler}",
                exc_info=True,
            )

    def process_queue(self) -> None:
        logger.info("🔄 EventHub dispatcher thread running...")

        while not self._stop_event.is_set():
            try:
                msg = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if msg is None:
                continue

            # Batch-drain quickly to reduce overhead under load. This preserves
            # the order messages are DISPATCHED (_deliver is called FIFO); it
            # does NOT order HANDLER execution — _deliver submits each handler to
            # the worker pool, so handlers (across messages and across one
            # message's fan-out) run concurrently and finish in no guaranteed
            # order. Subscribers that need ordering own their own queue (e.g.
            # EmiEventRelay for socket_emit / TTS).
            batch: List[Message] = [msg]
            while True:
                try:
                    nxt = self.queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    continue
                batch.append(nxt)

            for message in batch:
                self._deliver(message)

    def default_handler(self, message: Message) -> None:
        """Handles unknown or unregistered events."""
        if message.event_topic not in ["settings_changed"]:
            logger.warning(f"⚠️ No handler registered for event: {message.event_topic}")

    def list_registered_events(self) -> None:
        logger.debug("=== Registered Events in Hub ===")
        with self._lock:
            items = list(self.event_registry.items())
        if not items:
            logger.debug("No events have been registered yet.")
        else:
            for event_key, handlers in items:
                logger.debug(f"Event: {event_key}, Subscribers: {len(handlers)}")
        logger.debug("==================================")

    def stop(self) -> None:
        """Stops the dispatcher thread and worker pool."""
        self._stop_event.set()
        self.queue.put(None)
        self.worker_thread.join(timeout=2.0)
        self._executor.shutdown(wait=False)
        logger.info("🛑 EventHub stopped.")
