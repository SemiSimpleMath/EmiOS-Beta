from __future__ import annotations

import os
import time
from dataclasses import dataclass

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TwilioConfig:
    account_sid: str
    auth_token: str


class TwilioSmsService:
    """
    Minimal Twilio SMS sender.

    Env vars:
    - TWILIO_ACCOUNT_SID
    - TWILIO_AUTH_TOKEN

    Note: `from_number` is supplied per-send so you can support multiple inbound numbers.
    """

    def __init__(self, config: TwilioConfig | None = None):
        if config is None:
            config = TwilioConfig(
                account_sid=str(os.environ.get("TWILIO_ACCOUNT_SID") or "").strip(),
                auth_token=str(os.environ.get("TWILIO_AUTH_TOKEN") or "").strip(),
            )
        self._config = config

        if not self._config.account_sid or not self._config.auth_token:
            raise RuntimeError(
                "Twilio SMS not configured. Set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN."
            )

        try:
            from twilio.rest import Client  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Twilio package not installed. Install with `pip install twilio`."
            ) from e

        self._client = Client(self._config.account_sid, self._config.auth_token)

    def send_sms(self, *, to_number: str, from_number: str, body: str) -> str:
        to_number = (to_number or "").strip()
        from_number = (from_number or "").strip()
        body = (body or "").strip()
        if not to_number or not from_number or not body:
            raise ValueError("Missing to_number/from_number/body for SMS send.")

        # Retry on Twilio's 429 and transient 5xx with simple exponential
        # backoff. Twilio's Python SDK raises TwilioRestException carrying
        # the HTTP status code.
        try:
            from twilio.base.exceptions import TwilioRestException  # type: ignore
        except Exception:  # pragma: no cover
            TwilioRestException = Exception  # type: ignore

        backoff_s = [0.5, 1.5, 3.5]
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                msg = self._client.messages.create(
                    to=to_number,
                    from_=from_number,
                    body=body,
                )
                return str(getattr(msg, "sid", "") or "")
            except TwilioRestException as e:
                status = getattr(e, "status", 0) or 0
                last_exc = e
                if status == 429 or (500 <= status < 600):
                    if attempt < len(backoff_s):
                        delay = backoff_s[attempt]
                        logger.warning(
                            "Twilio send_sms transient status=%s attempt=%d/3; sleeping %.1fs.",
                            status, attempt + 1, delay,
                        )
                        time.sleep(delay)
                        continue
                raise
        raise RuntimeError(f"Twilio send_sms exhausted retries: {last_exc}") from last_exc

