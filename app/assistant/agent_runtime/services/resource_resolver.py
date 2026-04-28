from __future__ import annotations

from typing import Any

from app.resource_manager.resource_manager import ResourceManager
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class ResourceResolver:
    """
    Canonical runtime resource resolver.

    Instantiate with an explicit resource_manager (via AgentComponentsFactory).
    The legacy static entry-point is kept as a shim for callers (ContextInjector,
    UserBioContextService) that are not yet on the instance path; those callers
    remain responsible for their own DI lookup until they are migrated.
    """

    def __init__(self, *, resource_manager: ResourceManager) -> None:
        self._resource_manager = resource_manager

    def resolve(self, resource_id: str, *, required: bool = False, scope_context: Any = None) -> Any:
        """Instance method — use this from code that has a ResourceResolver injected."""
        return ResourceResolver._resolve(self._resource_manager, resource_id, required=required, scope_context=scope_context)

    # ---------------------------------------------------------------------------
    # Legacy static shim — do NOT add new call sites here.
    # Migrate existing callers to inject a ResourceResolver instance instead.
    # ---------------------------------------------------------------------------
    @staticmethod
    def get_global_resource(resource_id: str, *, required: bool = False, scope_context: Any = None) -> Any:
        from app.assistant.ServiceLocator.service_locator import DI  # local import — shim only
        resource_manager = getattr(DI, "resource_manager", None)
        if resource_manager is None:
            raise RuntimeError("DI.resource_manager is not available while resolving resources.")
        return ResourceResolver._resolve(resource_manager, resource_id, required=required, scope_context=scope_context)

    @staticmethod
    def _resolve(resource_manager: ResourceManager, resource_id: str, *, required: bool, scope_context: Any) -> Any:
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")
        if scope_context is None:
            raise ValueError("scope_context is required for scoped resource resolution.")
        try:
            return resource_manager.get_resource(
                scope_context=scope_context,
                resource_id=rid,
                required=required,
            )
        except PermissionError as e:
            logger.debug(
                "Scoped access denied for resource '%s' (required=%s): %s",
                rid,
                required,
                e,
            )
            raise
        except Exception as e:
            logger.error("Failed reading canonical resource '%s' from resource_manager: %s", rid, e)
            logger.debug("scoped resource read exception details", exc_info=True)
            raise
