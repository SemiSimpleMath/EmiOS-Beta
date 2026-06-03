
import sys

# Ensure the project root is in the path for imports
from pathlib import Path

from app.assistant.agent_registry.agent_factory import AgentFactory
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.event_hub.event_hub import EventHub
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.manager_registry.manager_registry import ManagerRegistry
from app.assistant.multi_agent_manager_factory.MultiAgentManagerFactory import MultiAgentManagerFactory
from app.assistant.ServiceLocator.service_locator import DI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANAGER_BASE_DIR = PROJECT_ROOT / "multi_agents"
MANAGER_CLASS_DIR = "app.assistant.manager_classes"
ORCHESTRATOR_BASE_DIR = PROJECT_ROOT / "orchestrators"

from app.assistant.ServiceLocator.service_locator import ServiceLocator
from app.assistant.lib.data_conversion_module.DataConversion import DataConversionModule

# Initialize logging
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

def initialize_services():
    """Registers core services in the ServiceLocator."""
    logger.info("🔧 Initializing core services for testing...")
    
    # Only create event_hub if it doesn't already exist (avoid overwriting the Flask app's instance)
    if not hasattr(DI, 'event_hub') or DI.event_hub is None:
        ServiceLocator.register('event_hub', EventHub())
    
    # Initialize user settings manager
    from app.assistant.user_settings_manager.user_settings import UserSettingsManager
    user_settings = UserSettingsManager()
    ServiceLocator.register('user_settings', user_settings)
    logger.info("✅ User settings manager initialized")

    # Apply persisted logging controls (console threshold + per-logger custom levels)
    # This is normally done in Flask create_app(), but tests bypass that bootstrap.
    try:
        from app.assistant.utils.logging_config import apply_logging_controls
        console_level = user_settings.get("logging.console_level", "WARNING")
        overrides = user_settings.get("logging.overrides", {}) or {}
        apply_logging_controls(console_level=console_level, overrides=overrides)
        logger.info("✅ Applied saved logging controls for tests")
    except Exception as e:
        logger.warning(f"⚠️ Could not apply saved logging controls for tests: {e}")
    
    ServiceLocator.register('data_conversion_module', DataConversionModule())
    ServiceLocator.register('agent_registry', AgentRegistry())
    ServiceLocator.register('tool_registry', ToolRegistry())
    DI.tool_registry.load_tools()
    # MCP metadata + cached tools (if present)
    DI.tool_registry.load_mcp_servers()
    DI.tool_registry.load_mcp_tool_cache(enabled_only=True)
    DI.agent_registry.load_agents()
    ServiceLocator.register('agent_factory', AgentFactory(agent_registry=DI.agent_registry, tool_registry=DI.tool_registry))
    ServiceLocator.register('manager_registry', ManagerRegistry(MANAGER_BASE_DIR))
    # Ensure manager configs are loaded for tests that spawn managers (including orchestrators).
    DI.manager_registry.preload_all()
    ServiceLocator.register('multi_agent_manager_factory', MultiAgentManagerFactory())

    # Orchestrators (parallel to managers)
    from app.assistant.orchestrator_registry.orchestrator_registry import OrchestratorRegistry
    from app.assistant.orchestrator_registry.orchestrator_instance_handler import OrchestratorInstanceHandler
    from app.assistant.orchestrator_classes.OrchestratorFactory import OrchestratorFactory

    orchestrator_registry = OrchestratorRegistry(ORCHESTRATOR_BASE_DIR)
    ServiceLocator.register("orchestrator_registry", orchestrator_registry)
    orchestrator_registry.preload_all()
    ServiceLocator.register("orchestrator_instance_handler", OrchestratorInstanceHandler())
    ServiceLocator.register(
        "orchestrator_factory",
        OrchestratorFactory(
            registry=orchestrator_registry,
            tool_registry=DI.tool_registry,
            agent_registry=DI.agent_registry,
        ),
    )

    # Register manager_invoker so orchestrators can invoke child managers.
    from app.assistant.manager_runtime.manager_invoker import ManagerInvoker
    ServiceLocator.register("manager_invoker", ManagerInvoker())
    
    # Initialize global blackboard
    from app.assistant.global_blackboard.global_blackboard import GlobalBlackBoard
    global_blackboard = GlobalBlackBoard()
    ServiceLocator.register("global_blackboard", global_blackboard)
    logger.info("✅ Global blackboard initialized")
    
    # Initialize ResourceManager and auto-load all resources
    from app.resource_manager.resource_manager import ResourceManager
    resource_manager = ResourceManager()
    ServiceLocator.register("resource_manager", resource_manager)
    resource_manager.load_all_from_directory("resources")

    # Initialize SkillRegistry (agentskills.io SKILL.md loader)
    from app.skill_registry import SkillInjector, SkillRegistry
    _skill_reg = SkillRegistry()
    ServiceLocator.register("skill_registry", _skill_reg)
    ServiceLocator.register("skill_injector", SkillInjector(_skill_reg))
    
    # Initialize Entity Catalog for fast entity detection
    from app.assistant.entity_management.entity_catalog import EntityCatalog
    entity_catalog = EntityCatalog.instance()
    ServiceLocator.register("entity_catalog", entity_catalog)
    logger.info("✅ Entity catalog initialized")

    # Initialize ticket_manager (required by tools that write tickets, e.g. send_email approval)
    from app.assistant.ticket_manager import get_ticket_manager, initialize_tickets_db
    initialize_tickets_db()
    ServiceLocator.register('ticket_manager', get_ticket_manager())
    logger.info("✅ Ticket manager initialized")

    # Wire AgentComponentsFactory (required by Agent.__init__ for context injection)
    from app.assistant.agent_runtime.factories.agent_components_factory import AgentComponentsFactory
    agent_components_factory = AgentComponentsFactory(
        event_hub=DI.event_hub,
        resource_manager=DI.resource_manager,
        blackboard=DI.global_blackboard,
    )
    ServiceLocator.register("agent_components_factory", agent_components_factory)
    logger.info("✅ AgentComponentsFactory initialized")

    # TaskIR timer scheduling requires DI.scheduler (production wires SchedulerService in
    # bootstrap.py). Tests need a faithful double: TaskIRTimerManager only touches
    # DI.scheduler.timing_engine.scheduler and arms real date-triggered jobs, so a running
    # APScheduler BackgroundScheduler is the minimal thing that both satisfies
    # _require_scheduler() and actually fires timers (relative-timer auto-wake tests).
    if not hasattr(DI, "scheduler") or DI.scheduler is None:
        import atexit
        from types import SimpleNamespace
        from datetime import timezone as _utc_tz
        from apscheduler.schedulers.background import BackgroundScheduler
        _test_bg_scheduler = BackgroundScheduler(timezone=_utc_tz.utc)
        _test_bg_scheduler.start()
        atexit.register(lambda: _test_bg_scheduler.shutdown(wait=False))
        ServiceLocator.register(
            "scheduler",
            SimpleNamespace(timing_engine=SimpleNamespace(scheduler=_test_bg_scheduler)),
        )
        logger.info("✅ Test scheduler (BackgroundScheduler) initialized")

    logger.info("✅ Test services initialized.")


initialize_services()