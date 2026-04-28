"""
Google OAuth Routes
Handles OAuth flow for Gmail, Calendar, Tasks, and Nest (SDM) access.

Scopes, credential file paths, and account definitions are driven by the
OAuthRegistry (configs/oauth_accounts.json) — no hardcoded scope lists here.
"""
from flask import Blueprint, redirect, url_for, request, jsonify, session
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from urllib.parse import urlencode
import json
import os
import secrets

from app.assistant.lib.google_auth.account_ids import (
    DEFAULT_GOOGLE_ACCOUNT_ID,
    GMAIL_GOOGLE_ACCOUNT_ID,
    CALENDAR_GOOGLE_ACCOUNT_ID,
    TASKS_GOOGLE_ACCOUNT_ID,
    NEST_GOOGLE_ACCOUNT_ID,
)
from app.assistant.lib.google_auth import oauth_registry
from app.assistant.lib.google_auth.oauth_account_store import get_google_oauth_account_store
from app.assistant.utils.logging_config import get_logger

if os.environ.get('APP_ENV', 'development') != 'production':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

google_oauth_bp = Blueprint('google_oauth', __name__)
logger = get_logger(__name__)

def _requested_account_id() -> str:
    account_id = str(request.args.get("account_id") or request.form.get("account_id") or "").strip()
    return account_id or DEFAULT_GOOGLE_ACCOUNT_ID


def _resolve_for_account(account_id: str, scope_set_hint: str | None = None):
    """Return (credentials_path, scopes, scope_set) for *account_id*.

    If the account is in the registry, we use its definitions.
    *scope_set_hint* is only used as a fallback for ad-hoc accounts that are
    NOT in the registry (the "Connect Additional Account" flow).
    """
    if oauth_registry.is_known_account(account_id):
        cred_path = oauth_registry.get_client_credentials_path(account_id)
        scopes = oauth_registry.get_scopes(account_id)
        scope_set = oauth_registry.get_scope_set(account_id)
        return cred_path, scopes, scope_set

    # Ad-hoc / additional account — fall back to workspace defaults
    scope_set = str(scope_set_hint or "workspace").strip()
    if not oauth_registry.is_known_account(DEFAULT_GOOGLE_ACCOUNT_ID):
        raise ValueError(
            f"Account '{account_id}' is not in the OAuth registry and "
            f"default account '{DEFAULT_GOOGLE_ACCOUNT_ID}' is also missing."
        )
    cred_path = oauth_registry.get_client_credentials_path(DEFAULT_GOOGLE_ACCOUNT_ID)
    default_acct = oauth_registry.get_account(DEFAULT_GOOGLE_ACCOUNT_ID)
    scopes = default_acct.get("scopes", [])
    return cred_path, scopes, scope_set


_NEST_PARTNER_AUTH_URL = "https://nestservices.google.com/partnerconnections/{project_id}/auth"


def _build_nest_authorization_url(
    credentials_path, scopes: list[str], redirect_uri: str, account_id: str,
) -> tuple[str, str]:
    """Build the Nest Device Access partner connections authorization URL.

    Unlike the standard Google OAuth URL, this endpoint presents the
    device/home selection screen that links Nest devices to the project.
    Returns (authorization_url, state).
    """
    nest_project_id = oauth_registry.get_nest_project_id(account_id)
    if not nest_project_id:
        raise ValueError(
            f"Account '{account_id}' has scope_set=nest but no nest_project_id configured."
        )

    cred_data = json.loads(credentials_path.read_text(encoding="utf-8"))
    client_config = cred_data.get("web") or cred_data.get("installed") or {}
    client_id = str(client_config.get("client_id") or "").strip()
    if not client_id:
        raise ValueError(
            f"Cannot read client_id from {credentials_path} for Nest partner auth."
        )

    state = secrets.token_urlsafe(32)
    params = {
        "redirect_uri": redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
        "client_id": client_id,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    base_url = _NEST_PARTNER_AUTH_URL.format(project_id=nest_project_id)
    authorization_url = f"{base_url}?{urlencode(params)}"
    return authorization_url, state


def _is_account_configured(account_id: str) -> bool:
    store = get_google_oauth_account_store()
    row = store.get_credentials(account_id=account_id)
    return row is not None

@google_oauth_bp.route('/api/oauth/google/status', methods=['GET'])
def oauth_status():
    """Check OAuth status for a specific account_id."""
    account_id = _requested_account_id()
    configured = _is_account_configured(account_id)
    try:
        cred_path, _, _ = _resolve_for_account(account_id)
        credentials_exist = cred_path.exists()
    except Exception as e:
        logger.debug("Could not resolve credentials for status check: %s", e)
        credentials_exist = False
    return jsonify(
        {
            'success': True,
            'account_id': account_id,
            'configured': configured,
            'credentials_exist': credentials_exist,
            'token_exists': configured,
        }
    )


@google_oauth_bp.route('/api/oauth/google/accounts', methods=['GET'])
def oauth_accounts():
    """List all known Google OAuth accounts."""
    try:
        store = get_google_oauth_account_store()
        rows = store.list_accounts()
        return jsonify({"success": True, "accounts": rows})
    except Exception as e:
        logger.error("Failed to list OAuth accounts: %s", e)
        logger.debug("oauth_accounts exception details", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


@google_oauth_bp.route('/api/oauth/google/config', methods=['GET'])
def oauth_config():
    """Return effective Google OAuth account routing IDs and registry metadata."""
    try:
        registry_accounts = {}
        for aid, cfg in oauth_registry.list_accounts().items():
            registry_accounts[aid] = {
                "display_name": cfg.get("display_name", aid),
                "scope_set": cfg.get("scope_set", ""),
                "provider": cfg.get("provider", ""),
            }
        return jsonify(
            {
                "success": True,
                "accounts": {
                    "default": DEFAULT_GOOGLE_ACCOUNT_ID,
                    "gmail": GMAIL_GOOGLE_ACCOUNT_ID,
                    "calendar": CALENDAR_GOOGLE_ACCOUNT_ID,
                    "tasks": TASKS_GOOGLE_ACCOUNT_ID,
                    "nest": NEST_GOOGLE_ACCOUNT_ID,
                },
                "registry": registry_accounts,
            }
        )
    except Exception as e:
        logger.error("Failed to load OAuth config: %s", e)
        logger.debug("oauth_config exception details", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500

@google_oauth_bp.route('/api/oauth/google/start', methods=['GET'])
def start_oauth():
    """Start the OAuth flow (JSON response for JS callers)."""
    try:
        account_id = _requested_account_id()
        scope_set_hint = str(request.args.get("scope_set") or "").strip() or None
        credentials_path, scopes, scope_set = _resolve_for_account(account_id, scope_set_hint)

        if not credentials_path.exists():
            return jsonify({
                'success': False,
                'error': (
                    f'OAuth client credentials not found for account {account_id!r}: '
                    f'{credentials_path}. Download it from Google Cloud Console.'
                ),
            }), 400

        redirect_uri = url_for('google_oauth.oauth_callback', _external=True)

        if scope_set == "nest":
            authorization_url, state = _build_nest_authorization_url(
                credentials_path, scopes, redirect_uri, account_id,
            )
        else:
            flow = Flow.from_client_secrets_file(
                str(credentials_path),
                scopes=scopes,
                redirect_uri=redirect_uri
            )
            authorization_url, state = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='false',
                prompt='consent',
            )

        session['oauth_state'] = state
        session['oauth_account_id'] = account_id
        session['oauth_scope_set'] = scope_set

        redirect_after = request.args.get('redirect_to', 'features_settings')
        session['oauth_redirect_after'] = redirect_after

        logger.info(
            "OAuth flow started: account_id=%s scope_set=%s cred_file=%s",
            account_id, scope_set, credentials_path.name,
        )

        return jsonify({
            'success': True,
            'authorization_url': authorization_url,
            'account_id': account_id,
        })

    except Exception as e:
        logger.error("Failed to start OAuth flow: %s", e)
        logger.debug("start_oauth exception details", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Failed to start OAuth flow: {str(e)}'
        }), 500


@google_oauth_bp.route('/oauth/google/start', methods=['GET'])
def start_oauth_redirect():
    """
    Start the OAuth flow and immediately redirect the browser to Google.

    This is the browser-friendly variant used for "auto-open" flows when a token
    is stale/revoked (invalid_grant). The /api/ version returns JSON for JS callers.
    """
    try:
        account_id = _requested_account_id()
        scope_set_hint = str(request.args.get("scope_set") or "").strip() or None
        credentials_path, scopes, scope_set = _resolve_for_account(account_id, scope_set_hint)

        if not credentials_path.exists():
            logger.error(
                "OAuth client credentials missing for account %s: %s",
                account_id, credentials_path,
            )
            return redirect(url_for('preferences.features_page') + '?oauth_error=missing_credentials')

        redirect_uri = url_for('google_oauth.oauth_callback', _external=True)

        if scope_set == "nest":
            authorization_url, state = _build_nest_authorization_url(
                credentials_path, scopes, redirect_uri, account_id,
            )
        else:
            flow = Flow.from_client_secrets_file(
                str(credentials_path),
                scopes=scopes,
                redirect_uri=redirect_uri,
            )
            authorization_url, state = flow.authorization_url(
                access_type='offline',
                include_granted_scopes='false',
                prompt='consent',
            )

        session['oauth_state'] = state
        session['oauth_account_id'] = account_id
        session['oauth_scope_set'] = scope_set
        redirect_after = request.args.get('redirect_to', 'features_settings')
        session['oauth_redirect_after'] = redirect_after

        logger.info(
            "OAuth browser flow started: account_id=%s scope_set=%s cred_file=%s",
            account_id, scope_set, credentials_path.name,
        )

        return redirect(authorization_url)
    except Exception as e:
        logger.error("Failed to start browser OAuth flow: %s", e)
        logger.debug("start_oauth_redirect exception details", exc_info=True)
        return redirect(url_for('preferences.features_page') + '?oauth_error=start_failed')

@google_oauth_bp.route('/oauth/google/callback')
def oauth_callback():
    """Handle OAuth callback."""
    try:
        state = session.get('oauth_state')
        if not state:
            return "Error: Invalid session state", 400
        account_id = str(session.get('oauth_account_id') or "").strip() or DEFAULT_GOOGLE_ACCOUNT_ID
        scope_set_hint = str(session.get('oauth_scope_set') or "").strip() or None
        credentials_path, scopes, scope_set = _resolve_for_account(account_id, scope_set_hint)

        redirect_uri = url_for('google_oauth.oauth_callback', _external=True)

        flow = Flow.from_client_secrets_file(
            str(credentials_path),
            scopes=scopes,
            state=state,
            redirect_uri=redirect_uri
        )

        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials

        creds_json = credentials.to_json()
        creds_dict = json.loads(creds_json)
        principal_email = str(creds_dict.get("account") or "").strip() or None
        store = get_google_oauth_account_store()
        store.upsert_credentials(
            account_id=account_id,
            principal_email=principal_email,
            granted_scopes=list(credentials.scopes or []),
            credentials_json=creds_json,
        )
        logger.info(
            "Google OAuth token stored. account_id=%s scope_set=%s cred_file=%s principal=%s",
            account_id, scope_set, credentials_path.name, principal_email or "unknown",
        )

        session.pop('oauth_state', None)
        session.pop('oauth_account_id', None)
        session.pop('oauth_scope_set', None)

        redirect_to = session.pop('oauth_redirect_after', 'features_settings')

        if redirect_to == 'setup':
            return redirect(url_for('setup.setup_wizard') + '?oauth_success=true')
        elif redirect_to == 'google_oauth_settings':
            return redirect(url_for('preferences.google_oauth_settings_page') + '?oauth_success=true')
        elif redirect_to == 'google_accounts_settings':
            return redirect(url_for('preferences.google_accounts_settings_page') + '?oauth_success=true')
        elif redirect_to == 'nest_settings':
            return redirect(url_for('preferences.nest_settings_page') + '?oauth_success=true')
        elif redirect_to in ('integrations_settings', 'integrations'):
            return redirect(url_for('preferences.integrations_hub_page') + '?oauth_success=true')
        else:
            return redirect(url_for('preferences.features_page') + '?oauth_success=true')

    except Exception as e:
        logger.error("OAuth callback failed: %s", e)
        logger.debug("oauth_callback exception details", exc_info=True)
        return f"Error during OAuth: {str(e)}", 500

@google_oauth_bp.route('/api/oauth/google/revoke', methods=['POST'])
def revoke_oauth():
    """Revoke OAuth token (disconnect Google account)"""
    try:
        payload = request.get_json(silent=True) or {}
        account_id = str(payload.get("account_id") or request.args.get("account_id") or "").strip()
        if not account_id:
            account_id = DEFAULT_GOOGLE_ACCOUNT_ID

        store = get_google_oauth_account_store()
        row = store.get_credentials(account_id=account_id)
        if row:
            try:
                creds_obj = Credentials.from_authorized_user_info(json.loads(str(row["credentials_json"])))
                token_value = str(getattr(creds_obj, "token", "") or "").strip()
                if token_value:
                    import requests

                    requests.post(
                        'https://oauth2.googleapis.com/revoke',
                        params={'token': token_value},
                        headers={'content-type': 'application/x-www-form-urlencoded'},
                        timeout=10,
                    )
            except Exception as e:
                logger.error("Could not revoke Google token remotely for account_id=%s: %s", account_id, e)
                logger.debug("revoke_oauth remote revoke exception details", exc_info=True)

        store.revoke(account_id=account_id)
        return jsonify({
            'success': True,
            'account_id': account_id,
            'message': 'Google account disconnected successfully'
        })
        
    except Exception as e:
        logger.error("Failed to revoke OAuth: %s", e)
        logger.debug("revoke_oauth exception details", exc_info=True)
        return jsonify({
            'success': False,
            'error': f'Failed to revoke OAuth: {str(e)}'
        }), 500

