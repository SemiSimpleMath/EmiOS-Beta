# cache.py

import threading

from typing import Optional, Type, Dict

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class ClassCache:
    """
    Caches loaded agent classes to avoid redundant imports.
    """
    def __init__(self):
        self._cache: Dict[str, Type] = {}
        self._lock = threading.Lock()

    def get_class(self, class_name: str) -> Optional[Type]:
        with self._lock:
            return self._cache.get(class_name)

    def cache_class(self, class_name: str, cls: Type):
        with self._lock:
            if class_name in self._cache:
                logger.debug(f"Class '{class_name}' is already cached. Overwriting.")
            self._cache[class_name] = cls
            logger.debug(f"Class '{class_name}' cached successfully.")

class TemplateCache:
    """
    Caches loaded templates to avoid redundant file reads.
    """
    def __init__(self):
        self._cache: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get_template(self, template_path: str) -> Optional[str]:
        with self._lock:
            return self._cache.get(template_path)

    def cache_template(self, template_path: str, template_content: str):
        with self._lock:
            if template_path in self._cache:
                logger.debug(f"Template '{template_path}' is already cached. Overwriting.")
            self._cache[template_path] = template_content
            logger.debug(f"Template '{template_path}' cached successfully.")
