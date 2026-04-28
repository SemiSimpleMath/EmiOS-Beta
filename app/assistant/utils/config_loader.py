# config_loader.py

import os
import re
from pathlib import Path
import yaml
from typing import Dict, Any, Tuple

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

#
# NOTE: Configs are edited frequently during development. We cache for performance,
# but we must invalidate when the file changes on disk (mtime).
#
config_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

def load_config(config_path: str) -> Dict[str, Any]:
    """
    Loads a YAML configuration file, replaces environment variables, and caches the result.

    Parameters:
    - config_path (str): Path to the YAML configuration file.

    Returns:
    - Dict[str, Any]: Parsed configuration dictionary.
    """
    path = Path(config_path)
    mtime = path.stat().st_mtime if path.exists() else 0.0
    cached = config_cache.get(config_path)
    if cached:
        cached_mtime, cached_config = cached
        if cached_mtime == mtime:
            return cached_config

    try:
        with open(config_path, 'r', encoding='utf-8') as file:
            content = file.read()
            # Replace environment variables like ${VAR_NAME} with their values; raise on missing.
            def _replace_env_var(m: re.Match) -> str:
                var_name = m.group(1)
                value = os.environ.get(var_name)
                if value is None:
                    logger.error(
                        "Config file %s references ${%s} but the variable is not set in the environment.",
                        config_path, var_name,
                    )
                    raise EnvironmentError(
                        f"Required environment variable '${{{var_name}}}' is not set "
                        f"(referenced in {config_path})."
                    )
                return value

            content = re.sub(r'\$\{(\w+)\}', _replace_env_var, content)
            config = yaml.safe_load(content)
            # Refresh cache with current mtime
            config_cache[config_path] = (mtime, config)
            logger.info(f"Configuration loaded and cached: {config_path}")
            return config
    except Exception as e:
        logger.error(f"Failed to load configuration from {config_path}: {e}")
        raise
