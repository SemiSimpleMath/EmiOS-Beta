from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.exc import SQLAlchemyError

from app.models.base import Base, get_session
from app.assistant.lib.google_auth import oauth_registry
from app.assistant.lib.google_auth.oauth_crypto import decrypt_text, encrypt_text
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


class GoogleOAuthAccount(Base):
    __tablename__ = "google_oauth_accounts"

    id = Column(Integer, primary_key=True)
    account_id = Column(String(128), unique=True, nullable=False, index=True)
    provider = Column(String(32), nullable=False, default="google")
    principal_email = Column(String(320), nullable=True)
    granted_scopes_json = Column(Text, nullable=False, default="[]")
    credentials_encrypted = Column(Text, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _safe_scopes_json(scopes: list[str] | None) -> str:
    return json.dumps(list(scopes or []), ensure_ascii=False)


def initialize_google_oauth_accounts_db() -> None:
    session = get_session()
    try:
        engine = session.bind
        Base.metadata.create_all(engine, tables=[GoogleOAuthAccount.__table__], checkfirst=True)
        logger.info("✅ Google OAuth accounts table initialized")
    except Exception as e:
        logger.error("Failed to initialize Google OAuth accounts table: %s", e)
        logger.debug("Google OAuth accounts table init exception details", exc_info=True)
        raise
    finally:
        session.close()


class GoogleOAuthAccountStore:
    def upsert_credentials(
        self,
        *,
        account_id: str,
        principal_email: str | None,
        granted_scopes: list[str],
        credentials_json: str,
    ) -> None:
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ValueError("account_id is required.")
        if not oauth_registry.is_known_account(account_id):
            known = sorted(oauth_registry.list_accounts().keys())
            raise ValueError(
                f"Refusing to upsert credentials for unregistered account_id={account_id!r}. "
                f"Add it to configs/oauth_accounts.json first. Known accounts: {known}."
            )
        if not isinstance(credentials_json, str) or not credentials_json.strip():
            raise ValueError("credentials_json is required.")
        session = get_session()
        try:
            row = (
                session.query(GoogleOAuthAccount)
                .filter(GoogleOAuthAccount.account_id == account_id)
                .one_or_none()
            )
            now = _now_utc()
            encrypted = encrypt_text(credentials_json)
            scopes_json = _safe_scopes_json(granted_scopes)
            if row is None:
                row = GoogleOAuthAccount()
                row.account_id = account_id
                row.provider = "google"
                row.created_at = now
                session.add(row)
            row.principal_email = str(principal_email or "").strip() or None
            row.granted_scopes_json = scopes_json
            row.credentials_encrypted = encrypted
            row.is_active = True
            row.updated_at = now
            row.last_refreshed_at = now
            row.revoked_at = None
            session.commit()
        except SQLAlchemyError as e:
            session.rollback()
            logger.error("Failed to upsert Google OAuth credentials for %s: %s", account_id, e)
            logger.debug("Google OAuth upsert SQLAlchemy exception details", exc_info=True)
            raise
        except Exception as e:
            session.rollback()
            logger.error("Failed to upsert Google OAuth credentials for %s: %s", account_id, e)
            logger.debug("Google OAuth upsert exception details", exc_info=True)
            raise
        finally:
            session.close()

    def get_credentials(self, *, account_id: str) -> dict[str, Any] | None:
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ValueError("account_id is required.")
        session = get_session()
        try:
            row = (
                session.query(GoogleOAuthAccount)
                .filter(
                    GoogleOAuthAccount.account_id == account_id,
                    GoogleOAuthAccount.is_active.is_(True),
                )
                .one_or_none()
            )
            if row is None:
                return None
            if not isinstance(row.credentials_encrypted, str) or not row.credentials_encrypted.strip():
                raise ValueError(f"Stored credentials are empty for account_id={account_id!r}")
            scopes = json.loads(row.granted_scopes_json or "[]")
            if not isinstance(scopes, list):
                raise ValueError(f"Stored scopes payload is invalid for account_id={account_id!r}")
            return {
                "account_id": account_id,
                "principal_email": row.principal_email,
                "granted_scopes": [str(s) for s in scopes if isinstance(s, str)],
                "credentials_json": decrypt_text(row.credentials_encrypted),
                "updated_at": row.updated_at,
                "last_refreshed_at": row.last_refreshed_at,
            }
        except Exception as e:
            logger.error("Failed to load Google OAuth credentials for %s: %s", account_id, e)
            logger.debug("Google OAuth get_credentials exception details", exc_info=True)
            raise
        finally:
            session.close()

    def revoke(self, *, account_id: str) -> bool:
        account_id = str(account_id or "").strip()
        if not account_id:
            raise ValueError("account_id is required.")
        session = get_session()
        try:
            row = (
                session.query(GoogleOAuthAccount)
                .filter(GoogleOAuthAccount.account_id == account_id)
                .one_or_none()
            )
            if row is None:
                return False
            row.is_active = False
            row.revoked_at = _now_utc()
            row.updated_at = _now_utc()
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logger.error("Failed to revoke Google OAuth account %s: %s", account_id, e)
            logger.debug("Google OAuth revoke exception details", exc_info=True)
            raise
        finally:
            session.close()

    def list_accounts(self) -> list[dict[str, Any]]:
        session = get_session()
        try:
            rows = session.query(GoogleOAuthAccount).order_by(GoogleOAuthAccount.account_id.asc()).all()
            out: list[dict[str, Any]] = []
            for row in rows:
                scopes_raw = json.loads(row.granted_scopes_json or "[]")
                scopes = [str(s) for s in scopes_raw if isinstance(s, str)] if isinstance(scopes_raw, list) else []
                out.append(
                    {
                        "account_id": row.account_id,
                        "principal_email": row.principal_email,
                        "granted_scopes": scopes,
                        "is_active": bool(row.is_active),
                        "created_at": row.created_at.isoformat() if row.created_at else None,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                        "last_refreshed_at": row.last_refreshed_at.isoformat() if row.last_refreshed_at else None,
                        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
                    }
                )
            return out
        except Exception as e:
            logger.error("Failed to list Google OAuth accounts: %s", e)
            logger.debug("Google OAuth list_accounts exception details", exc_info=True)
            raise
        finally:
            session.close()


_store: GoogleOAuthAccountStore | None = None


def get_google_oauth_account_store() -> GoogleOAuthAccountStore:
    global _store
    if _store is None:
        _store = GoogleOAuthAccountStore()
    return _store

