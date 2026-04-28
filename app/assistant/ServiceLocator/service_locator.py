# service_locator.py
import threading
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class ServiceLocator:
    _services = {}
    _lock = threading.Lock()

    @classmethod
    def register(cls, name: str, service: object) -> None:
        with cls._lock:
            cls._services[name] = service

    @classmethod
    def get(cls, name: str) -> object:
        with cls._lock:
            return cls._services.get(name)

class DIProxy:
    def __getattr__(self, name: str) -> object:
        service = ServiceLocator.get(name)
        if service is None:
            raise AttributeError(f"Service '{name}' not registered.")
        return service

# Global DI instance for convenience
DI = DIProxy()
