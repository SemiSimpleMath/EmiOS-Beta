"""
Discover the authorized user's principal email from a Google Credentials object.

Modern google.oauth2.credentials.Credentials.to_json() does not include the
principal email — it only carries tokens, scopes, and client info. To bind a
credential set to a real Google identity we have to call an authenticated API
that exposes the user record.

Strategy: pick the probe based on which scope the credentials grant.
  - Gmail scope present  -> users().getProfile() returns emailAddress
  - else                 -> None (e.g. nest credentials don't authorize a user
                            in this sense; the principal is the Device Access
                            project, configured separately in oauth_accounts.json)

Probe failures are logged and return None. The principal_email field is
informational — features route on account_id, not principal_email — so a
missing value is acceptable. The next refresh re-attempts the probe.
"""
from __future__ import annotations

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

_GMAIL_SCOPES = frozenset({
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://mail.google.com/",
})


def fetch_principal_email(credentials: Credentials) -> str | None:
    """Return the authorized Google user's email, or None if no probe applies."""
    granted = set(credentials.scopes or [])
    if not (granted & _GMAIL_SCOPES):
        return None
    try:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        profile = service.users().getProfile(userId="me").execute()
        email = str(profile.get("emailAddress") or "").strip()
        return email or None
    except Exception as e:
        logger.warning("Principal email probe via gmail.getProfile failed: %s", e)
        logger.debug("Principal email probe exception details", exc_info=True)
        return None
