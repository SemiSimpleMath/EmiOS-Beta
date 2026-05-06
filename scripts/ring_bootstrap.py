"""One-time interactive bootstrap of Ring 2FA authentication.

Ring does not issue plain API keys. Auth is OAuth-style with 2FA: you log in
with your Ring username + password, Ring sends a one-time code via SMS or
email, and the resulting access/refresh token pair is what every subsequent
API call uses.

This script walks through that flow once and writes the resulting token to
``data/ring_token.json``. The Flask bridge (``app/routes/smart_home_bridge.py``)
then loads that token on each call and silently rotates it as Ring's refresh
mechanism kicks in (the ``token_updater`` callback writes the rotated token
back to the same file).

Usage:
    .venv/Scripts/python.exe scripts/ring_bootstrap.py

You will be prompted for username, password, and the 2FA code Ring sends.
The resulting token is the only credential the bridge ever sees — your
password is not stored anywhere.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATH = REPO_ROOT / "data" / "ring_token.json"
USER_AGENT = "EmiOS/1.0"


async def _bootstrap() -> dict:
    try:
        from ring_doorbell import Auth
    except ImportError:
        sys.stderr.write(
            "ring-doorbell is not installed. Run:\n"
            "    .venv/Scripts/pip install ring-doorbell\n"
        )
        sys.exit(1)

    print("Ring authentication bootstrap")
    print("-----------------------------")
    print("This is a one-time login. Your password is NOT stored.")
    print()
    username = input("Ring email: ").strip()
    if not username:
        sys.stderr.write("Empty username — aborting.\n")
        sys.exit(1)
    password = getpass.getpass("Ring password: ")
    if not password:
        sys.stderr.write("Empty password — aborting.\n")
        sys.exit(1)

    captured: dict = {}

    def _on_token(token: dict) -> None:
        captured.update(token)

    auth = Auth(USER_AGENT, token_updater=_on_token)

    def _otp_prompt() -> str:
        return input("Enter the 2FA code Ring just sent (SMS/email): ").strip()

    # ring-doorbell's Auth.fetch_token() raises Requires2FAError on the first
    # call, then accepts the OTP on the second call. The library API for this
    # has shifted across versions — we try the modern shape first and fall
    # back to the older one-shot signature if needed.
    try:
        try:
            await auth.async_fetch_token(username, password, _otp_prompt())
        except TypeError:
            # Older API shape: fetch_token takes only (user, password) and
            # raises Requires2FAError carrying a continuation.
            from ring_doorbell.auth import Requires2FAError
            try:
                await auth.async_fetch_token(username, password)
            except Requires2FAError:
                otp = _otp_prompt()
                await auth.async_fetch_token(username, password, otp)
    except Exception as exc:
        sys.stderr.write(f"\nLogin failed: {exc}\n")
        sys.exit(1)

    if not captured:
        # Some library versions don't fire token_updater on initial fetch.
        # Pull the token directly off the auth object.
        token = getattr(auth, "_token", None) or getattr(auth, "token", None)
        if isinstance(token, dict):
            captured.update(token)
    if not captured:
        sys.stderr.write(
            "Login appeared to succeed but no token was returned. "
            "Check the ring-doorbell version installed.\n"
        )
        sys.exit(1)
    return captured


def main() -> None:
    token = asyncio.run(_bootstrap())
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(token, indent=2, sort_keys=True), encoding="utf-8")
    print()
    print(f"Token written to {TOKEN_PATH}")
    print("You can now use the Ring controls in the EmiOS UI.")


if __name__ == "__main__":
    main()
