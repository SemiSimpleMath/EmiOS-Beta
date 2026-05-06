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

    from ring_doorbell import Requires2FAError

    captured: dict = {}

    def _on_token(token: dict) -> None:
        captured.update(token)

    auth = Auth(USER_AGENT, token_updater=_on_token)

    # Step 1: send credentials WITHOUT an OTP. If Ring requires 2FA, this is
    # the call that causes Ring to send the SMS/email — and it raises
    # Requires2FAError. Only after that should we prompt the user.
    try:
        token = await auth.async_fetch_token(username, password)
    except Requires2FAError:
        print()
        print("Ring sent a 2FA code via SMS or email. Check your phone/inbox.")
        otp = input("Enter the 2FA code: ").strip()
        if not otp:
            sys.stderr.write("Empty 2FA code — aborting.\n")
            sys.exit(1)
        try:
            token = await auth.async_fetch_token(username, password, otp)
        except Exception as exc:
            sys.stderr.write(f"\n2FA verification failed: {exc}\n")
            sys.exit(1)
    except Exception as exc:
        sys.stderr.write(f"\nLogin failed: {exc}\n")
        sys.exit(1)

    # Prefer the token returned directly by fetch_token; fall back to whatever
    # the token_updater captured.
    if isinstance(token, dict) and token:
        captured.update(token)
    if not captured:
        sys.stderr.write(
            "Login appeared to succeed but no token was returned.\n"
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
