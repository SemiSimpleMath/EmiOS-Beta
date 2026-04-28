from __future__ import annotations

import json
import pickle
from pathlib import Path

from app.assistant.lib.google_auth.account_ids import DEFAULT_GOOGLE_ACCOUNT_ID
from app.assistant.lib.google_auth import oauth_registry
from app.assistant.lib.google_auth.oauth_account_store import (
    get_google_oauth_account_store,
    initialize_google_oauth_accounts_db,
)

_CREDENTIALS_DIR = Path(__file__).resolve().parent.parent / "credentials"


def run_migration(account_id: str = DEFAULT_GOOGLE_ACCOUNT_ID) -> None:
    token_pickle = _CREDENTIALS_DIR / "token.pickle"
    if not token_pickle.exists():
        raise FileNotFoundError(f"Legacy token file not found: {token_pickle}")
    cred_path = oauth_registry.get_client_credentials_path(account_id)
    if not cred_path.exists():
        raise FileNotFoundError(f"OAuth client credentials file not found: {cred_path}")

    with open(token_pickle, "rb") as f:
        creds = pickle.load(f)
    creds_json = creds.to_json()
    creds_dict = json.loads(creds_json)
    principal_email = str(creds_dict.get("account") or "").strip() or None
    initialize_google_oauth_accounts_db()
    store = get_google_oauth_account_store()
    store.upsert_credentials(
        account_id=account_id,
        principal_email=principal_email,
        granted_scopes=list(getattr(creds, "scopes", []) or []),
        credentials_json=creds_json,
    )
    print(f"[OK] Migrated legacy token.pickle -> account_id={account_id}")


if __name__ == "__main__":
    run_migration()

