"""
work_objects.runtime_setup — register the standard manager services the minimal
test bootstrap omits, so the STANDARD path (manager_invoker -> scope_adapter ->
sub-manager) works instead of the hand-rolled request_handler + scope hack.

Only mam_instance_manager is missing after test_setup (manager_invoker,
resource_manager, the manager factory, and scope_adapter-on-the-invoker are all
present). The full app registers this in app/assistant/initialize_system.py.
"""
from __future__ import annotations

from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator

_done = False


def ensure_manager_services() -> None:
    global _done
    if _done:
        return
    # The minimal test bootstrap does not load .env (run_flask.py does). Load it
    # here so tools that read os.getenv at import time (e.g. search_web's
    # GOOGLE_SEARCH_API_KEY) see the configured values during scenario runs.
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    if getattr(DI, "mam_instance_manager", None) is None:
        from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
        ServiceLocator.register("mam_instance_manager", MAMInstanceManager(resource_manager=DI.resource_manager))
    # The standard manager-test pattern (emi_team_test.py): preload all manager
    # configs/agents up front so nothing loads lazily mid-run — which is when the
    # un-deepcopy-able state appeared that broke create_manager(web_manager).
    DI.manager_registry.preload_all()
    _done = True
