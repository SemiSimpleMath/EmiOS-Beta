from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def _fernet() -> Fernet:
    key = str(os.getenv("EMI_GOOGLE_OAUTH_ENCRYPTION_KEY") or "").strip()
    if not key:
        raise ValueError(
            "EMI_GOOGLE_OAUTH_ENCRYPTION_KEY is required for Google OAuth token encryption."
        )
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as e:
        logger.error("Invalid EMI_GOOGLE_OAUTH_ENCRYPTION_KEY: %s", e)
        logger.debug("OAuth encryption key parse exception details", exc_info=True)
        raise ValueError("EMI_GOOGLE_OAUTH_ENCRYPTION_KEY is invalid for Fernet.") from e


def encrypt_text(plaintext: str) -> str:
    if not isinstance(plaintext, str) or not plaintext:
        raise ValueError("plaintext must be a non-empty string.")
    token = _fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_text(ciphertext: str) -> str:
    if not isinstance(ciphertext, str) or not ciphertext:
        raise ValueError("ciphertext must be a non-empty string.")
    try:
        data = _fernet().decrypt(ciphertext.encode("utf-8"))
    except InvalidToken as e:
        logger.error("Google OAuth token decryption failed: invalid token.")
        logger.debug("OAuth token decryption exception details", exc_info=True)
        raise ValueError("Unable to decrypt Google OAuth credentials (invalid token).") from e
    except Exception as e:
        logger.error("Google OAuth token decryption failed: %s", e)
        logger.debug("OAuth token decryption exception details", exc_info=True)
        raise
    return data.decode("utf-8")

