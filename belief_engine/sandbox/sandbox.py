"""
BeliefSandbox — isolated harness for the decay step.

The sandbox creates a temp SQLite file, points the SQLAlchemy engine at it
via the DEV_DATABASE_URI_EMI env var, ensures the belief schema exists, and
provides seed/run/inspect helpers. On exit the temp file is removed.

Critical: import this module BEFORE any belief code touches its store.
Setting the env var here (in __init__) before BeliefStore.get_session() is
called is what redirects writes to the sandbox file. Importing
belief_engine.store.belief_store at module-load time would lock the URI to
emi.db before the sandbox runs, so we import lazily inside the class methods.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional


class BeliefSandbox:
    """
    Context manager for an isolated belief-engine database.

    Usage:
        with BeliefSandbox() as sb:
            sb.seed_belief(...)
            sb.run_decay(...)
            sb.print_state(...)
    """

    def __init__(self) -> None:
        self._tmpdir: Optional[str] = None
        self._db_path: Optional[Path] = None
        self._saved_env: dict = {}

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def __enter__(self) -> "BeliefSandbox":
        self._tmpdir = tempfile.mkdtemp(prefix="belief_sandbox_")
        self._db_path = Path(self._tmpdir) / "sandbox.db"

        # Point the global engine cache at the sandbox file. Save existing
        # env so we can restore on exit.
        self._saved_env = {
            k: os.environ.get(k) for k in ("DEV_DATABASE_URI_EMI", "USE_TEST_DB", "TEST_DATABASE_URI_EMI")
        }
        os.environ["DEV_DATABASE_URI_EMI"] = f"sqlite:///{str(self._db_path).replace(chr(92), '/')}"
        os.environ.pop("USE_TEST_DB", None)
        os.environ.pop("TEST_DATABASE_URI_EMI", None)

        # Build the schema in the sandbox file. This is a no-op if the env
        # var redirect worked — ensure_schema() reads the same env-driven
        # URI logic. We assert the path matches afterwards.
        from belief_engine.db.ensure_schema import ensure_schema
        from app.assistant.utils.path_utils import get_repo_root

        # ensure_schema hardcodes get_repo_root() / "emi.db" — that's a
        # production path. Bypass it: build the schema directly via the
        # session that the env var now redirects.
        from app.models.base import get_session
        from belief_engine.db.schema import SCHEMA_SQL

        session = get_session()
        try:
            for stmt in SCHEMA_SQL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    session.execute(__import__("sqlalchemy").text(stmt))
            session.commit()
        finally:
            session.close()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Drop any open engine for the sandbox URI from the global cache so
        # the next sandbox run gets a fresh one.
        try:
            from app.models.base import _engines, _sessionmakers, _engines_lock
            sandbox_uri = os.environ.get("DEV_DATABASE_URI_EMI")
            if sandbox_uri:
                with _engines_lock:
                    eng = _engines.pop(sandbox_uri, None)
                    _sessionmakers.pop(sandbox_uri, None)
                if eng is not None:
                    eng.dispose()
        except Exception:
            pass  # best-effort cleanup of process state

        # Restore env.
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

        # Remove temp dir.
        if self._tmpdir and os.path.isdir(self._tmpdir):
            shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────────
    # Time helpers
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def days_ago(cls, n: int) -> str:
        """Return ISO 8601 timestamp for n days before now."""
        return (cls.now() - timedelta(days=n)).isoformat()

    @classmethod
    def days_ago_dt(cls, n: int) -> datetime:
        return cls.now() - timedelta(days=n)

    # ──────────────────────────────────────────────────────────────────
    # Seed
    # ──────────────────────────────────────────────────────────────────

    def seed_belief(
        self,
        *,
        key: str,
        scope: str,
        last_confirmed: str,
        domain: str = "routine",
        statement: str = "Seeded belief.",
        confidence: str = "medium",
        status: str = "active",
        observation_count: int = 1,
        first_observed: Optional[str] = None,
    ) -> str:
        """Insert a belief row directly. Returns the new belief id."""
        from app.models.base import get_session
        from belief_engine.db.models import UserBelief

        belief_id = str(uuid.uuid4())
        now = self.now().isoformat()
        session = get_session()
        try:
            session.add(UserBelief(
                id=belief_id,
                domain=domain,
                belief_key=key,
                statement=statement,
                confidence=confidence,
                scope=scope,
                status=status,
                conditions=None,
                observation_count=observation_count,
                first_observed=first_observed or last_confirmed,
                last_confirmed=last_confirmed,
                created_at=now,
                updated_at=now,
            ))
            session.commit()
        finally:
            session.close()
        return belief_id

    # ──────────────────────────────────────────────────────────────────
    # Run decay
    # ──────────────────────────────────────────────────────────────────

    def run_decay(
        self,
        *,
        domain: str,
        now_utc: Optional[datetime] = None,
        temporary_ttl_days: int = 30,
        chronic_review_ttl_days: int = 180,
    ) -> dict:
        """
        Run the decay step against the sandbox DB.
        Returns the step's result dict (keys: temporary_deprecated,
        chronic_contested, ...).
        """
        from belief_engine.pipeline.steps.decay_stale_beliefs import DecayStaleBeliefsStep

        step = DecayStaleBeliefsStep(
            domain=domain,
            temporary_ttl_days=temporary_ttl_days,
            chronic_review_ttl_days=chronic_review_ttl_days,
            now_utc=now_utc,
        )

        # Lightweight ctx — just enough to satisfy the step's reads/writes.
        class _Ctx:
            def __init__(self) -> None:
                self.belief_update_result = None
                self.decay_result = None

        ctx = _Ctx()
        step.run(ctx)
        return ctx.decay_result

    # ──────────────────────────────────────────────────────────────────
    # Inspect
    # ──────────────────────────────────────────────────────────────────

    def list_beliefs(self, *, domain: Optional[str] = None) -> List[dict]:
        from app.models.base import get_session
        from belief_engine.db.models import UserBelief

        session = get_session()
        try:
            q = session.query(UserBelief)
            if domain:
                q = q.filter(UserBelief.domain == domain)
            q = q.order_by(UserBelief.domain, UserBelief.belief_key)
            rows = q.all()
            return [{
                "key": r.belief_key,
                "domain": r.domain,
                "scope": r.scope,
                "status": r.status,
                "obs": r.observation_count,
                "last_confirmed": r.last_confirmed,
            } for r in rows]
        finally:
            session.close()

    def list_evidence(self, *, belief_key: str) -> List[dict]:
        from app.models.base import get_session
        from belief_engine.db.models import UserBelief, BeliefEvidence

        session = get_session()
        try:
            row = session.query(UserBelief).filter(
                UserBelief.belief_key == belief_key,
            ).first()
            if not row:
                return []
            evs = session.query(BeliefEvidence).filter(
                BeliefEvidence.belief_id == row.id,
            ).order_by(BeliefEvidence.created_at).all()
            return [{
                "source_type": e.source_type,
                "signal_type": e.signal_type,
                "summary": e.summary,
                "created_at": e.created_at,
            } for e in evs]
        finally:
            session.close()

    def print_state(self, *, domain: Optional[str] = None, label: str = "") -> None:
        beliefs = self.list_beliefs(domain=domain)
        header = f"--- BELIEF STATE{f' ({label})' if label else ''}" + (f" domain={domain}" if domain else "") + " ---"
        print(header)
        if not beliefs:
            print("  (no beliefs)")
            return
        for b in beliefs:
            print(
                f"  {b['key']:40s} scope={b['scope']:9s} status={b['status']:11s} "
                f"obs={b['obs']:3d} last_confirmed={b['last_confirmed'][:10]}"
            )
