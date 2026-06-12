# app/assistant/initialize_system.py

import threading
import time


from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator
from app.assistant.emi_event_relay.emi_event_relay import EmiEventRelay
from app.assistant.agent_runtime.services.question_service import QuestionService
from app.assistant.progress_curator import ProgressCurator
from app.assistant.chat_narrator import ChatNarrator
from app.assistant.signal_router import SignalRouterService
from app.assistant.task_ir_runtime import get_task_ir_runner
from app.services.socket_manager import SocketManager
from app.assistant.validation.agent_validator import validate_all

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)


def initialize_system():

    logger.info("initialize_system() is running...")
    logger.info(f"Active threads count at start of initialize: {threading.active_count()}")
    preload_start = time.time()
    manager_registry = DI.manager_registry
    manager_registry.preload_all()
    preload_end = time.time()
    elapsed_time = preload_end - preload_start

    logger.info(f"Preloading completed in {elapsed_time:.2f} seconds.")

    # Build the display-name registry from each manager config's
    # ``display_name:`` field. Used by the chat narrator + @mention router.
    from app.assistant.chat_narrator.display_names import (
        initialize_display_name_registry,
    )
    display_names: dict[str, str] = {}
    for mgr_name, mgr_cfg in (manager_registry.all_configs() or {}).items():
        if not isinstance(mgr_cfg, dict):
            continue
        dn = mgr_cfg.get("display_name")
        if isinstance(dn, str) and dn.strip():
            display_names[mgr_name] = dn.strip()
    initialize_display_name_registry(display_names)
    logger.info(
        "Display-name registry initialized with %d named managers: %s",
        len(display_names), sorted(display_names.values()),
    )

    agent_registry = DI.agent_registry
    validate_all(agent_registry)


    # Create and register the SocketManager
    socket_manager = SocketManager()
    ServiceLocator.register('socket_manager', socket_manager)

    # Relay AFK state changes to the music client (frontend decides whether to pause/resume).
    from app.assistant.afk_manager.music_afk_relay import MusicAfkRelay
    ServiceLocator.register("music_afk_relay", MusicAfkRelay())

    event_relay = EmiEventRelay()
    ServiceLocator.register('event_relay', event_relay)

    from app.assistant.ticket_manager.ticket_dispatch import TicketDispatcherRegistry
    from app.assistant.ticket_manager.ticket_dispatch.adapters.socketio import (
        SocketIOTicketAdapter,
    )
    from app.assistant.ticket_manager.ticket_dispatch.adapters.telegram import (
        TelegramTicketAdapter,
    )
    from app.assistant.ticket_manager.ticket_dispatch.adapters.slack import (
        SlackTicketAdapter,
    )
    from app.assistant.ticket_manager.ticket_dispatch.adapters.sms import (
        SmsTicketAdapter,
    )
    ticket_dispatcher = TicketDispatcherRegistry()
    ticket_dispatcher.register(SocketIOTicketAdapter())
    ticket_dispatcher.register(TelegramTicketAdapter())
    ticket_dispatcher.register(SlackTicketAdapter())
    ticket_dispatcher.register(SmsTicketAdapter())
    ticket_dispatcher.subscribe_to_event_hub(DI.event_hub)
    ServiceLocator.register('ticket_dispatcher', ticket_dispatcher)

    progress_curator = ProgressCurator()
    ServiceLocator.register("progress_curator", progress_curator)
    chat_narrator = ChatNarrator()
    ServiceLocator.register("chat_narrator", chat_narrator)
    from app.assistant.manager_runtime.mailbox import Mailbox
    ServiceLocator.register("mailbox", Mailbox())
    from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
    ServiceLocator.register("mam_instance_manager", MAMInstanceManager(
        resource_manager=DI.resource_manager,
    ))
    from app.assistant.chat_outbound import OutboundChatPublisher
    ServiceLocator.register("outbound_chat_publisher", OutboundChatPublisher())
    ServiceLocator.register("question_service", QuestionService())

    logger.info("Loading emi_result_handler...")
    emi_result_handler = DI.agent_factory.create_agent('emi_result_handler', DI.global_blackboard)
    ServiceLocator.register('emi_result_handler', emi_result_handler)

    logger.info("Loading emi_reminder_handler...")
    emi_reminder_handler = DI.agent_factory.create_agent('emi_reminder_handler', DI.global_blackboard)
    ServiceLocator.register('emi_reminder_handler', emi_reminder_handler)

    # Camera dispatch is wired declaratively as the camera_dispatch routine
    # in configs/routines.json (trigger.type=event, topic=ring_snapshot_captured).
    # Subscriptions happen inside RoutineManager.refresh(), but the
    # background-task scheduler doesn't run that for the first ~45s after
    # boot. Without an explicit refresh here the very first ring_snapshot_captured
    # event published in that window has no subscribers and silently drops.
    # A one-shot refresh at bootstrap closes that gap for every event-triggered
    # routine, not just camera_dispatch.
    try:
        from app.assistant.utils.subsystem_flags import is_subsystem_enabled
        if is_subsystem_enabled("routine_manager"):
            from app.assistant.routine_manager import get_routine_manager
            get_routine_manager().refresh()
            logger.info("RoutineManager initial refresh complete; event triggers wired.")
    except Exception as e:
        logger.warning("Initial RoutineManager refresh failed: %s", e)

    # Slack inbound is handled exclusively by the /slack/events webhook
    # (app/routes/slack_events.py). The legacy SlackInterface polling
    # adapter was retired 2026-05-05 — running both paths caused
    # duplicate-message processing.

    logger.info(f"Active threads count at end of initialize: {threading.active_count()}")

    from app.assistant.utils.subsystem_flags import is_subsystem_enabled

    if is_subsystem_enabled("signal_router"):
        signal_router = SignalRouterService(emit_to_event_hub=True)
        ServiceLocator.register("signal_router", signal_router)
        logger.info("✅ Signal router initialized (subscriber mode — driven by the gut)")
    else:
        logger.info("⏸️ Signal router DISABLED via subsystems.yaml")

    # The gut — unified inbound intake. Signal_router subscribes for reactive
    # watches; pod_classifier (if enabled) subscribes for declarative pod
    # minting. Both are wired in before the gut starts so no envelopes are
    # dispatched into an empty subscriber list.
    if is_subsystem_enabled("ingest"):
        from app.assistant.ingest import IngestService
        from app.assistant.ingest.sources import EmailRepoSource, UnifiedLogSource

        ingest_service = IngestService(
            sources=[UnifiedLogSource(), EmailRepoSource()],
            poll_interval_seconds=120,
        )
        ServiceLocator.register("ingest_service", ingest_service)

        signal_router = getattr(DI, "signal_router", None)
        if signal_router is not None:
            ingest_service.register_subscriber(signal_router.handle_envelope)
            logger.info("✅ Ingest: signal_router.handle_envelope subscribed")
        else:
            logger.info("Ingest: signal_router not present; no subscribers wired")

        if is_subsystem_enabled("pod_classifier"):
            from app.assistant.pod_store.pod_classifier_service import PodClassifierService
            pod_classifier_service = PodClassifierService()
            ServiceLocator.register("pod_classifier_service", pod_classifier_service)
            ingest_service.register_subscriber(pod_classifier_service.handle_envelope)
            pod_classifier_service.start()
            logger.info("✅ Ingest: pod_classifier subscribed (cold start — no consumers yet)")
        else:
            logger.info("⏸️ PodClassifier DISABLED via subsystems.yaml")

        ingest_service.start()
        logger.info("✅ Ingest service initialized")
    else:
        logger.info("⏸️ Ingest service DISABLED via subsystems.yaml")

    task_ir_runner = get_task_ir_runner()
    ServiceLocator.register("task_ir_runner", task_ir_runner)
    task_ir_runner.ensure_event_subscription()
    logger.info("✅ Initialized task_ir_runner")

    # Route ticket responses for ticket-mode pending questions back into
    # the subconscious answer loop (mark answered → annotate concern →
    # trigger a noticer tick).
    from app.assistant.pending_questions.ticket_delivery import register_ticket_answer_listener
    register_ticket_answer_listener()

    if is_subsystem_enabled("dayflow_scheduler"):
        from app.assistant.dayflow_orchestrator.dayflow_scheduler import DayflowScheduler
        dayflow_scheduler = DayflowScheduler(
            timing_engine=DI.scheduler.timing_engine,
            app=DI.scheduler.app,
        )
        ServiceLocator.register("dayflow_scheduler", dayflow_scheduler)
        dayflow_scheduler.start()
        logger.info("✅ DayflowScheduler initialized")
    else:
        logger.info("⏸️ DayflowScheduler DISABLED via subsystems.yaml")

    return
