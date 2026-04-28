from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, request

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TelegramBotConfig:
    bot_token: str
    send_max_attempts: int = 3


class TelegramBotService:
    """
    Minimal Telegram Bot API sender.

    Required env vars:
    - TELEGRAM_BOT_TOKEN
    """

    def __init__(self, config: TelegramBotConfig | None = None):
        if config is None:
            raw_attempts = str(os.environ.get("TELEGRAM_SEND_MAX_ATTEMPTS") or "3").strip()
            try:
                attempts = int(raw_attempts)
            except Exception as e:
                logger.debug("Invalid TELEGRAM_SEND_MAX_ATTEMPTS=%r; using default=3. error=%s", raw_attempts, e, exc_info=True)
                attempts = 3
            if attempts < 1:
                attempts = 1
            if attempts > 4:
                attempts = 4
            config = TelegramBotConfig(
                bot_token=str(os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip(),
                send_max_attempts=attempts,
            )
        self._config = config
        if not self._config.bot_token:
            raise RuntimeError("Telegram bot is not configured. Set TELEGRAM_BOT_TOKEN.")

    def send_message(self, *, chat_id: str, text: str) -> dict[str, Any]:
        chat_id = str(chat_id or "").strip()
        text = str(text or "").strip()
        if not chat_id or not text:
            raise ValueError("Missing chat_id/text for Telegram send.")

        url = f"https://api.telegram.org/bot{self._config.bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        max_attempts = int(self._config.send_max_attempts or 3)
        backoff_s = [0.4, 1.0, 2.0]
        raw = ""
        for attempt in range(1, max_attempts + 1):
            try:
                with request.urlopen(req, timeout=20) as resp:  # noqa: S310
                    raw = resp.read().decode("utf-8", errors="replace")
                break
            except error.HTTPError as e:
                err_body = ""
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception as read_err:
                    logger.debug("Telegram HTTPError body read failed: %s", read_err, exc_info=True)
                # Telegram returns 429 with a Retry-After header when rate-limited.
                # Respect it and retry rather than hard-failing the caller.
                if e.code == 429 and attempt < max_attempts:
                    retry_after_raw = ""
                    try:
                        retry_after_raw = str(e.headers.get("Retry-After") or "").strip()
                    except Exception:
                        pass
                    try:
                        retry_after = float(retry_after_raw) if retry_after_raw else 1.0
                    except ValueError:
                        retry_after = 1.0
                    # Cap server-requested delay so a pathological response can't
                    # hang the worker for minutes.
                    retry_after = min(max(retry_after, 0.5), 30.0)
                    logger.warning(
                        "Telegram sendMessage 429 rate-limit on attempt %s/%s; sleeping %.1fs per Retry-After.",
                        attempt, max_attempts, retry_after,
                    )
                    time.sleep(retry_after)
                    continue
                logger.error("Telegram sendMessage HTTPError on attempt %s/%s: status=%s", attempt, max_attempts, e.code)
                raise RuntimeError(f"Telegram sendMessage HTTPError status={e.code} body={err_body[:500]}") from e
            except error.URLError as e:
                if attempt >= max_attempts:
                    logger.error("Telegram sendMessage URLError after %s/%s attempts: %s", attempt, max_attempts, e)
                    raise RuntimeError(f"Telegram sendMessage URLError: {e}") from e
                delay = backoff_s[min(attempt - 1, len(backoff_s) - 1)]
                logger.warning(
                    "Telegram sendMessage transient URLError on attempt %s/%s: %s. Retrying in %.1fs",
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                time.sleep(delay)
            except Exception as e:
                logger.error("Telegram sendMessage failed on attempt %s/%s: %s", attempt, max_attempts, e)
                logger.debug("Telegram sendMessage exception details", exc_info=True)
                raise RuntimeError(f"Telegram sendMessage failed: {e}") from e

        try:
            parsed = json.loads(raw)
        except Exception as e:
            logger.debug("Telegram sendMessage JSON parse failed. raw=%r error=%s", raw[:500], e, exc_info=True)
            raise RuntimeError("Telegram sendMessage returned non-JSON response.") from e
        if not isinstance(parsed, dict):
            raise RuntimeError("Telegram sendMessage returned invalid payload shape.")
        if not bool(parsed.get("ok", False)):
            raise RuntimeError(f"Telegram sendMessage returned ok=false payload={parsed}")
        return parsed

