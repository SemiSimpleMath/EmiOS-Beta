"""
Utility to get the assistant's name from configuration.

Resolution order:
  1. ASSISTANT_NAME environment variable (set via UI settings page or .env)
  2. resources/assistant/resource_assistant_data.json  name field
  3. Default: "Emi" — change via ASSISTANT_NAME env var or setup wizard
"""
import json
import os
from pathlib import Path

from app.assistant.utils.path_utils import get_resources_dir
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def get_assistant_name() -> str:
    """
    Return the assistant's name.

    Checks ASSISTANT_NAME env var first so the value set through the UI
    settings page takes effect without requiring a resource-file reload.
    Falls back to resource_assistant_data.json, then "Emi".
    """
    env_val = os.environ.get("ASSISTANT_NAME", "").strip()
    if env_val:
        return env_val

    try:
        resources_dir = get_resources_dir()
        assistant_data_file = resources_dir / "assistant" / "resource_assistant_data.json"

        if assistant_data_file.exists():
            with open(assistant_data_file, "r", encoding="utf-8") as f:
                assistant_data = json.load(f)
                name = (assistant_data.get("name") or "").strip()
                if name:
                    return name
    except Exception as e:
        logger.error("Could not load assistant name from resource file: %s", e, exc_info=True)

    return "Emi"


