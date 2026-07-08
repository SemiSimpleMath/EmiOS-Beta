"""Single source of truth for the belief-engine DB file path.

Belief tables live in the SAME db as the app. Resolve via the app's DB URI (honors
USE_TEST_DB / DEV_DATABASE_URI_EMI — and EMI_DATA_DIR once the packaging path refactor
lands) so belief and app always agree on which file they read and write. Every
belief-engine module that opens a raw sqlite3 connection resolves through here.
"""
from __future__ import annotations

import logging

from app.assistant.utils.path_utils import get_repo_root

logger = logging.getLogger(__name__)


def belief_db_path() -> str:
    """The sqlite file the belief tables live in — the app DB."""
    try:
        # Local import avoids an import cycle at package-load time.
        from app.models.base import get_database_uri
        uri = get_database_uri()
        if uri.startswith("sqlite:///"):
            return uri[len("sqlite:///"):]
    except Exception:
        logger.exception("[belief_engine] could not resolve app DB URI; using legacy path")
    return str(get_repo_root() / "emi.db")
