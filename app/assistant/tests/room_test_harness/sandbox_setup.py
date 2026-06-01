"""Sandboxed bootstrap for the e2e harness.

Calls the normal test_setup (registers DI services, loads tools/agents/managers,
boots the resource manager etc.) but FIRST redirects every SQLite DB the system
might write to into a per-process tempfile. The real ``emi.db`` / ``test_emidb.db``
is never touched.

Usage::

    with sandboxed_di() as sandbox:
        # all DI services are initialized; writes land in sandbox.db_path
        from app.assistant.ServiceLocator.service_locator import DI
        ...

Idempotent across a process: the second ``with`` call short-circuits to the
already-initialized DI (initializing services is expensive — ~5-10 seconds).
Tempfile cleanup happens on context exit.

Caveats:
- ChromaDB sandboxing is NOT yet wired here. KG-pipeline tests will write to
  the real chromadb path. Add chromadb tempdir redirection when KG e2e tests
  are needed.
- LLM calls are real. Cost ~$0.05-$0.50 per run depending on chain length.
- Outbound transport is NOT stubbed here — that's the harness's job
  (see ``recorders.py``).
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_INIT_LOCK = threading.Lock()
_INITIALIZED = False
_CURRENT_DB_PATH: Optional[Path] = None


@dataclass
class SandboxContext:
    """Handle for an active sandbox. Holds paths so tests can inspect post-mortem."""
    db_path: Path
    db_name: str  # the name passed via TEST_DB_NAME (without .db suffix)


def _make_tempfile_db_name() -> tuple[str, Path]:
    """Pick a unique per-process db name and predict its on-disk path.

    base.get_database_uri() builds the SQLite file under the repo root
    (three levels up from app/models/base.py) using ``TEST_DB_NAME`` as the
    filename when ``USE_TEST_DB=true``. We pick a name and mirror that
    path-construction so we can clean up the file afterward.
    """
    name = f"e2e_sandbox_{uuid.uuid4().hex[:12]}"
    # Mirror base.get_database_uri()'s path construction.
    repo_root = Path(__file__).resolve().parents[4]
    db_path = repo_root / f"{name}.db"
    return name, db_path


def _set_db_env(db_name: str) -> dict:
    """Set the env vars base.get_database_uri() consults. Returns prior values
    so the caller can restore."""
    prior = {
        "USE_TEST_DB": os.environ.get("USE_TEST_DB"),
        "TEST_DB_NAME": os.environ.get("TEST_DB_NAME"),
    }
    os.environ["USE_TEST_DB"] = "true"
    os.environ["TEST_DB_NAME"] = db_name
    return prior


def _restore_db_env(prior: dict) -> None:
    for key, value in prior.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _initialize_tables_for_sandbox() -> None:
    """Build schema for the e2e harness on the current (test) DB.

    Calls the non-Flask-bound table initializers individually. Skips the
    music tables — they're bound to flask_sqlalchemy and require an app
    context that the test harness doesn't have, and aren't relevant to
    room → manager → outbound flows anyway.
    """
    from app.database.table_initializer import (
        initialize_core_tables,
        initialize_entity_card_tables,
        initialize_news_tables,
    )
    initialize_core_tables()
    initialize_entity_card_tables()
    initialize_news_tables()


def _initialize_manager_runtime_services() -> None:
    """Register manager-runtime services that test_setup.py omits.

    test_setup.py covers the basics (registries, factories, agent components,
    resource manager). The harness needs more for end-to-end manager invocation:
    mam_instance_manager (tracks active manager invocations), mailbox, outbound
    publisher, chat_narrator, etc. Mirrors a subset of initialize_system.py.

    Idempotent — checks if a service is already registered before registering.
    """
    from app.assistant.ServiceLocator.service_locator import DI, ServiceLocator

    def _register_if_missing(name: str, build_fn):
        if not hasattr(DI, name) or getattr(DI, name) is None:
            try:
                ServiceLocator.register(name, build_fn())
            except Exception:
                # Defensive: a service may fail to construct in test env (e.g.,
                # missing config). Don't bring down the harness for an
                # optional service.
                pass

    # mailbox + mam_instance_manager are required by ManagerInvoker.invoke.
    from app.assistant.manager_runtime.mailbox import Mailbox
    _register_if_missing("mailbox", Mailbox)

    from app.assistant.manager_runtime.mam_instance_manager import MAMInstanceManager
    _register_if_missing(
        "mam_instance_manager",
        lambda: MAMInstanceManager(resource_manager=DI.resource_manager),
    )

    # Outbound + narrator — needed when manager publishes a final response.
    from app.assistant.chat_outbound import OutboundChatPublisher
    _register_if_missing("outbound_chat_publisher", OutboundChatPublisher)

    from app.assistant.chat_narrator import ChatNarrator
    _register_if_missing("chat_narrator", ChatNarrator)

    # progress_curator publishes agent_progress_fact events; some control
    # nodes assume it's around.
    try:
        from app.assistant.progress_curator import ProgressCurator
        _register_if_missing("progress_curator", ProgressCurator)
    except ImportError:
        pass

    # question_service powers the ask_user path on socketio/sms.
    try:
        from app.assistant.question_service import QuestionService
        _register_if_missing("question_service", QuestionService)
    except ImportError:
        pass


def _initialize_di_once() -> None:
    """Run the heavy DI bootstrap if it hasn't run yet in this process."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        # Importing test_setup triggers initialize_services() at module load.
        import app.assistant.tests.test_setup  # noqa: F401
        # Build schema on the (just-redirected) test DB. Music tables skipped
        # because they're Flask-SQLAlchemy bound; rest of the always-on set
        # uses plain SQLAlchemy and works outside an app context.
        _initialize_tables_for_sandbox()
        # Manager-runtime services needed for full pipeline invocation.
        _initialize_manager_runtime_services()
        _INITIALIZED = True


@contextmanager
def sandboxed_di() -> Iterator[SandboxContext]:
    """Context manager: redirect DB to a tempfile, init DI, yield a handle.

    On exit, deletes the tempfile DB. DI services stay alive for the rest
    of the process (so a second call reuses them with a NEW tempfile DB —
    note that the engine cache in app.models.base keys on URI, so the new
    URI gets a fresh engine automatically).

    Failure mode to be aware of: if a previous call left a stale entry in
    base._engines for the same URI (it won't happen with unique names) the
    schema would already exist. Tempfile names use uuid4 so collision risk
    is nil.
    """
    # SAFETY GUARD (entry): refuse only a sqlite:// TEST_DATABASE_URI_EMI override.
    # get_database_uri() honors TEST_DATABASE_URI_EMI ahead of TEST_DB_NAME, but
    # ONLY when it starts with "sqlite://" (this is the SQLite repo; non-sqlite
    # URIs are ignored and the path falls through to TEST_DB_NAME). So only a
    # sqlite override would actually bypass the tempfile sandbox onto a real/shared
    # DB while teardown deletes the unused tempfile. A non-sqlite URI (e.g. a
    # postgres test URI in the environment) is inert here — allow it.
    _explicit_uri = os.environ.get("TEST_DATABASE_URI_EMI")
    if _explicit_uri and str(_explicit_uri).startswith("sqlite://"):
        raise RuntimeError(
            "sandboxed_di refuses to run with a sqlite:// TEST_DATABASE_URI_EMI set "
            f"({_explicit_uri!r}) — get_database_uri() would honor it and bypass the "
            "tempfile sandbox, operating on a non-sandbox database. Unset it before "
            "running the harness."
        )

    db_name, db_path = _make_tempfile_db_name()
    prior_env = _set_db_env(db_name)
    # Force prompt + LLM-result logging ON for EVERY agent during harness runs.
    # EMI_PRINT_PROMPTS / EMI_PRINT_LLM_RESULTS are the built-in global overrides
    # checked FIRST in llm_client.resolve_prompt_debug_flags / resolve_llm_result_debug_flag,
    # so every agent — including ones with no per-agent prompt_debug config such as
    # room::switchboard — writes MODEL_SYSTEM / MODEL_USER / MODEL_OUTPUT to the
    # agent log. This is the existing logging path; no capture hooks needed.
    _prior_print = {
        "EMI_PRINT_PROMPTS": os.environ.get("EMI_PRINT_PROMPTS"),
        "EMI_PRINT_LLM_RESULTS": os.environ.get("EMI_PRINT_LLM_RESULTS"),
    }
    os.environ["EMI_PRINT_PROMPTS"] = "1"
    os.environ["EMI_PRINT_LLM_RESULTS"] = "1"
    global _CURRENT_DB_PATH
    _CURRENT_DB_PATH = db_path
    try:
        _initialize_di_once()
        # If this is NOT the first sandbox, the schema wasn't built for the
        # new URI yet — build it now. Idempotent (checkfirst=True on every
        # create_all), so re-running is safe.
        _initialize_tables_for_sandbox()
        yield SandboxContext(db_path=db_path, db_name=db_name)
    finally:
        # Dispose the cached SQLAlchemy engine for this URI so its connection
        # pool releases the file (Windows holds an exclusive lock otherwise).
        try:
            from app.models import base as _base_mod
            with _base_mod._engines_lock:
                uri = f"sqlite:///{str(db_path).replace(chr(92), '/')}"
                eng = _base_mod._engines.pop(uri, None)
                _base_mod._sessionmakers.pop(uri, None)
            if eng is not None:
                eng.dispose()
        except Exception:
            pass
        # SAFETY GUARD (teardown): only ever delete the uuid-named sandbox
        # tempfile. Refuse to unlink anything else — most importantly the real
        # emi.db — even if db_path were somehow corrupted. Safe by construction,
        # not by accident.
        _repo_root = Path(__file__).resolve().parents[4]
        _real_db = (_repo_root / "emi.db").resolve()
        _safe_to_delete = (
            db_path.name.startswith("e2e_sandbox_")
            and db_path.name.endswith(".db")
            and db_path.resolve() != _real_db
        )
        if not _safe_to_delete:
            logger.error(
                "[sandbox] REFUSING to delete non-sandbox DB path: %s (real=%s). "
                "Leaving it untouched.", db_path, _real_db,
            )
        else:
            # Best-effort cleanup. SQLite may hold a WAL/SHM sidecar.
            for suffix in ("", "-wal", "-shm"):
                try:
                    p = Path(str(db_path) + suffix)
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
        _restore_db_env(prior_env)
        for _k, _v in _prior_print.items():
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        _CURRENT_DB_PATH = None


def current_sandbox_db_path() -> Optional[Path]:
    """Return the active sandbox DB path, or None if not in a sandbox."""
    return _CURRENT_DB_PATH
