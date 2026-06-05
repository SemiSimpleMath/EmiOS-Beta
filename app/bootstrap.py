# app/bootstrap.py
import atexit
from pathlib import Path

from app.assistant.agent_registry.agent_factory import AgentFactory
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.manager_registry.manager_registry import ManagerRegistry
from app.assistant.multi_agent_manager_factory.MultiAgentManagerFactory import MultiAgentManagerFactory
from app.assistant.orchestrator_registry.orchestrator_registry import OrchestratorRegistry
from app.assistant.orchestrator_classes.OrchestratorFactory import OrchestratorFactory
from app.assistant.orchestrator_registry.orchestrator_instance_handler import OrchestratorInstanceHandler
from app.assistant.lib.data_conversion_module.DataConversion import DataConversionModule
from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
from app.resource_manager.resource_manager import ResourceManager
from app.assistant.ServiceLocator.service_locator import ServiceLocator, DI
from app.assistant.manager_runtime.manager_invoker import ManagerInvoker

from app.assistant.scheduler.orchestrator.scheduler_service import SchedulerService
from app.assistant.room_session_manager import RoomSessionManager

config_path = "app/assistant/scheduler/config/events_config.yaml"
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

from app.assistant.event_hub.event_hub import EventHub
import os


def _seed_personal_resources() -> None:
    """Copy .example -> .json for any personal resource files that don't exist yet.

    Tracked .example files ship with the repo as sanitized templates.
    Real .json files are gitignored and created here on first run
    (or by the setup wizard later with real user data).
    """
    from app.assistant.utils.path_utils import get_resources_dir
    import shutil

    resources_dir = get_resources_dir()
    example_pairs = [
        resources_dir / "user" / "resource_user_data.json",
        resources_dir / "user" / "resource_wiki_sections.json",
        resources_dir / "assistant" / "resource_assistant_data.json",
        resources_dir / "assistant" / "resource_assistant_personality_data.json",
        resources_dir / "assistant" / "resource_chat_guidelines_data.json",
        resources_dir / "assistant" / "resource_relationship_config.json",
        resources_dir / "assistant" / "assistant_core.json",
        resources_dir / "env_registry_user.json",
        resources_dir / "instructions" / "resource_orchestrator_user_prefs.md",
    ]

    for real_path in example_pairs:
        if real_path.exists():
            continue
        example_path = real_path.with_suffix(real_path.suffix + ".example")
        if not example_path.exists():
            logger.warning(
                "Personal resource missing and no .example template found: %s",
                real_path,
            )
            continue
        real_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(example_path, real_path)
        logger.info("Seeded personal resource from template: %s", real_path.name)


def _log_telegram_env_status() -> None:
    secret = str(os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()
    token = str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    logger.info(
        "Telegram env bootstrap check: TELEGRAM_WEBHOOK_SECRET=%s TELEGRAM_BOT_TOKEN=%s",
        "set" if secret else "missing",
        "set" if token else "missing",
    )


def _auto_detect_default_llm_provider() -> None:
    """Set DEFAULT_LLM_PROVIDER if not already set and only one provider has a key.

    This ensures the LLMFactory rerouting works out-of-the-box when a user
    only has one API key configured (e.g. OPENAI_API_KEY but no GOOGLE_API_KEY).
    """
    if os.environ.get("DEFAULT_LLM_PROVIDER", "").strip():
        return  # already explicitly configured

    from app.configs.llm_classes_dict import _key_is_present, _PROVIDER_DEFAULT_MODEL

    available = [p for p in ("openai", "gemini", "anthropic") if _key_is_present(p)]
    if len(available) == 1:
        provider = available[0]
        os.environ["DEFAULT_LLM_PROVIDER"] = provider
        os.environ.setdefault("DEFAULT_LLM_MODEL", _PROVIDER_DEFAULT_MODEL.get(provider, ""))
        logger.info(
            "Auto-detected single LLM provider: DEFAULT_LLM_PROVIDER=%s DEFAULT_LLM_MODEL=%s",
            provider,
            os.environ.get("DEFAULT_LLM_MODEL", ""),
        )
    elif len(available) == 0:
        logger.warning("No LLM provider API keys found. Agents will fail on LLM calls.")
    else:
        logger.info("Multiple LLM providers available (%s); no DEFAULT_LLM_PROVIDER override needed.", ", ".join(available))


def initialize_services(app):
    """
    Initializes core services like event bus, agent managers, data conversion, and scheduler.
    """

    # Auto-detect default LLM provider before any agents are loaded.
    _auto_detect_default_llm_provider()

    # Concurrency: event bus handler pool.
    # Default bumped to 24 (target range 20-30). Override via env:
    #   EMI_EVENT_WORKER_THREADS=20..30
    worker_threads = int(os.environ.get("EMI_EVENT_WORKER_THREADS", "24"))
    _log_telegram_env_status()
    event_hub = EventHub(worker_threads=worker_threads)
    ServiceLocator.register('event_hub', event_hub)

    # Register the DB write coordinator. Single point of write
    # serialization — see app/models/db_manager.py.
    from app.models.db_manager import get_db_manager
    ServiceLocator.register('db_manager', get_db_manager())
    logger.info("✅ DB manager registered (single-writer coordinator)")

    # Register user settings manager
    from app.assistant.user_settings_manager.user_settings import UserSettingsManager
    user_settings = UserSettingsManager()
    ServiceLocator.register('user_settings', user_settings)
    logger.info("✅ User settings manager initialized")

    # Initialize and start AFK monitor (standalone service, used by multiple consumers)
    from app.assistant.afk_manager import AFKMonitor
    afk_monitor = AFKMonitor()
    afk_monitor.start()
    ServiceLocator.register('afk_monitor', afk_monitor)
    logger.info("✅ AFK monitor started")

    ServiceLocator.register('agent_registry', AgentRegistry())
    ServiceLocator.register('tool_registry', ToolRegistry())
    DI.tool_registry.load_tools()
    DI.tool_registry.load_mcp_servers()
    DI.tool_registry.load_mcp_tool_cache(enabled_only=True)
    DI.tool_registry.load_installed_mcp_tools(enabled_only=True)
    DI.agent_registry.load_agents()
    ServiceLocator.register('agent_factory', AgentFactory(agent_registry=DI.agent_registry, tool_registry=DI.tool_registry))
    base_path = Path(__file__).resolve().parents[0] / "assistant" / "multi_agents"
    manager_registry = ManagerRegistry(base_path)
    ServiceLocator.register('manager_registry', manager_registry)
    manager_factory = MultiAgentManagerFactory()
    ServiceLocator.register("multi_agent_manager_factory", manager_factory)

    # Orchestrators (parallel to managers)
    orchestrators_path = Path(__file__).resolve().parents[0] / "assistant" / "orchestrators"
    orchestrator_registry = OrchestratorRegistry(orchestrators_path)
    ServiceLocator.register("orchestrator_registry", orchestrator_registry)
    orchestrator_registry.preload_all()
    ServiceLocator.register("orchestrator_instance_handler", OrchestratorInstanceHandler())
    orchestrator_factory = OrchestratorFactory(
        registry=orchestrator_registry,
        tool_registry=DI.tool_registry,
        agent_registry=DI.agent_registry,
    )
    ServiceLocator.register("orchestrator_factory", orchestrator_factory)

    data_conversion_module = DataConversionModule()
    ServiceLocator.register("data_conversion_module", data_conversion_module)

    global_blackboard = GlobalBlackBoard()
    ServiceLocator.register("global_blackboard", global_blackboard)

    # Seed personal resource files from .example templates on first run.
    _seed_personal_resources()

    # Initialize ResourceManager and auto-load all resources
    resource_manager = ResourceManager()
    ServiceLocator.register("resource_manager", resource_manager)
    resource_manager.load_all_from_directory("resources")
    # Dynamic resource: accounts — value COMPUTED per scope (principal + authority
    # filtered), gated by the standard resource path. Replaces the old
    # available_accounts auto-inject. See docs/architecture/SECRETS_ACCOUNTS.md.
    resource_manager.register_provider(
        "resource_accounts",
        lambda scope: DI.env_registry.render_accounts_for_scope(scope),
    )
    # Domain-scoped account views: an agent surfaces only the account CATEGORIES
    # relevant to its function (relevance, NOT permission — scope + authority still
    # gate access). personal_admin (email / calendar / todos) takes the email view,
    # so social accounts (bluesky, etc.) never enter its prompt.
    resource_manager.register_provider(
        "resource_email_accounts",
        lambda scope: DI.env_registry.render_accounts_for_scope(scope, categories={"email"}),
    )
    # The user's email identity is user-mode only — lock it to acting_as=user so it
    # does not leak into self/assistant mode (where email-as-self governs instead).
    # acting_as defaults to "user", so this is safe; only /actas self withholds it.
    resource_manager.register_lock("resource_user_email", {"acting_as": "user"})
    # Skills are durable, hand-authored knowledge files (e.g. "when working
    # with Documents on this machine, also check OneDrive"). They live in a
    # separate top-level dir from `resources/` (which is mostly data and
    # pipeline outputs) so the skill library is easy to curate and review.
    # They use the same loader so agents can reference them via context_items
    # the same way as resources (id = filename stem).
    # SkillRegistry — agentskills.io standard SKILL.md loader. Walks
    # `skills/<name>/SKILL.md` directories. See docs/architecture/21_SKILLS.md.
    from app.skill_registry import SkillInjector, SkillRegistry
    skill_registry = SkillRegistry()
    ServiceLocator.register("skill_registry", skill_registry)
    # SkillInjector — evaluates per-skill auto_inject_when triggers against
    # the current agent context. Replaces per-agent task_keyword_resources.
    ServiceLocator.register("skill_injector", SkillInjector(skill_registry))

    # Reply routing (multi-transport: socketio, sms, etc.)
    from app.services.reply_router import ReplyRouter
    reply_router = ReplyRouter()
    ServiceLocator.register("reply_router", reply_router)

    # Wire ManagerInvoker with explicit resource_manager dependency
    from app.assistant.manager_runtime.request_preprocessor import RequestPreprocessor
    ServiceLocator.register("manager_invoker", ManagerInvoker(
        preprocessor=RequestPreprocessor(resource_manager=resource_manager)
    ))

    # Wire AgentComponentsFactory with explicit dependencies
    from app.assistant.agent_runtime.factories.agent_components_factory import AgentComponentsFactory
    agent_components_factory = AgentComponentsFactory(
        event_hub=event_hub,
        resource_manager=resource_manager,
        blackboard=global_blackboard,
    )
    ServiceLocator.register("agent_components_factory", agent_components_factory)

    ServiceLocator.register("room_session_manager", RoomSessionManager(
        blackboard=global_blackboard,
        event_hub=event_hub,
        reply_router=reply_router,
        resource_manager=resource_manager,
        manager_registry=manager_registry,
        multi_agent_manager_factory=manager_factory,
        manager_invoker=DI.manager_invoker,
    ))

    # Initialize Entity Catalog for fast entity detection
    from app.assistant.entity_management.entity_catalog import EntityCatalog
    entity_catalog = EntityCatalog.instance()
    ServiceLocator.register("entity_catalog", entity_catalog)

    from app.assistant.env_registry import EnvRegistryService
    ServiceLocator.register("env_registry", EnvRegistryService())
    logger.info("✅ Entity catalog initialized")

    # Initialize scheduler service (auto-starts via TimingEngine.__init__)
    # APScheduler's BackgroundScheduler runs in its own background threads automatically
    scheduler_service = SchedulerService(app)
    app.scheduler_service = scheduler_service
    ServiceLocator.register('scheduler', scheduler_service)

    logger.info("EventHub started in the background.")

    # Initialize tickets table and manager
    from app.assistant.ticket_manager import get_ticket_manager, initialize_tickets_db
    from app.assistant.lib.google_auth import initialize_google_oauth_accounts_db
    initialize_google_oauth_accounts_db()
    initialize_tickets_db()
    ticket_manager = get_ticket_manager()
    ServiceLocator.register('ticket_manager', ticket_manager)
    logger.info("✅ Tickets table and manager initialized")
    
    # Clear stale tool approval tickets from previous session
    cleared = ticket_manager.clear_tickets(
        ticket_type='tool_approval',
        states=['pending', 'proposed']
    )
    if cleared > 0:
        logger.info(f"🧹 Cleared {cleared} stale tool approval tickets")

    from app.assistant.utils.subsystem_flags import is_subsystem_enabled

    # Initialize and start background task manager
    if is_subsystem_enabled("background_tasks"):
        from app.assistant.background_task_manager import start_background_tasks
        background_manager = start_background_tasks()
        ServiceLocator.register('background_task_manager', background_manager)
        logger.info("✅ Background task manager started")
    else:
        logger.info("⏸️ Background task manager DISABLED via subsystems.yaml")

    # Initialize DJ Manager (music automation)
    if is_subsystem_enabled("dj_manager"):
        from app.assistant.dj_manager import get_dj_manager
        dj_manager = get_dj_manager(app=app)
        ServiceLocator.register('dj_manager', dj_manager)
        dj_manager.start()
        logger.info("✅ DJ Manager initialized")
    else:
        logger.info("⏸️ DJ Manager DISABLED via subsystems.yaml")

    # Graceful shutdown: stop background services in LIFO order on process exit.
    # Prevents abandoned threads, in-flight LLM calls terminated mid-stream,
    # and SQLite locks surviving across restarts.
    atexit.register(shutdown_services)

    return DI


def shutdown_services() -> None:
    """Stop all background services cleanly. Safe to call multiple times."""
    logger.info("🛑 Shutting down background services...")

    def _safe_stop(label: str, fn):
        try:
            fn()
            logger.info("✅ %s stopped.", label)
        except Exception as e:
            logger.error("⚠️ %s stop failed: %s", label, e)
            logger.debug("%s stop exception", label, exc_info=True)

    # LIFO: stop consumers/emitters before the event bus they publish into.
    dj_manager = getattr(DI, "dj_manager", None)
    if dj_manager is not None:
        _safe_stop("DJ Manager", dj_manager.stop)

    if getattr(DI, "background_task_manager", None) is not None:
        from app.assistant.background_task_manager import stop_background_tasks
        _safe_stop("Background task manager", stop_background_tasks)

    scheduler = getattr(DI, "scheduler", None)
    if scheduler is not None:
        _safe_stop("Scheduler", scheduler.stop)

    afk_monitor = getattr(DI, "afk_monitor", None)
    if afk_monitor is not None:
        _safe_stop("AFK monitor", afk_monitor.stop)

    event_hub = getattr(DI, "event_hub", None)
    if event_hub is not None:
        _safe_stop("Event hub", event_hub.stop)

    # Dispose SQLite engines last so pending commits from above have a chance to land.
    try:
        from app.models.base import _engines  # type: ignore
        for uri, engine in list(_engines.items()):
            try:
                engine.dispose()
                logger.info("✅ Disposed SQLAlchemy engine: %s", uri)
            except Exception as e:
                logger.error("⚠️ Engine dispose failed for %s: %s", uri, e)
    except Exception:
        logger.debug("No _engines registry to dispose.", exc_info=True)

    logger.info("🛑 Shutdown complete.")
