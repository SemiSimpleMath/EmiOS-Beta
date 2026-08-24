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
from app.assistant.lib.google_auth.principal_resolver import fetch_principal_email
from app.assistant.utils.logging_config import get_logger

if os.environ.get('APP_ENV', 'development') != 'production':
    os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

google_oauth_bp = Blueprint('google_oauth', __name__)
logger = get_logger(__name__)

def _requested_account_id() -> str:
    account_id = str(request.args.get("account_id") or request.form.get("account_id") or "").strip()
    return account_id or DEFAULT_GOOGLE_ACCOUNT_ID


def _expected_identity(account_id: str) -> str:
    """The email an account_id is SUPPOSED to consent as (env-registry handle), or "".

    This is what makes a consent window self-identifying: the assistant's account and
    the owner's account are different Google identities, and Google's chooser cannot
    know which one we mean. Passed to Google as `login_hint` so the right account is
    pre-selected, and re-checked in the callback so a wrong-identity consent is
    REFUSED instead of stored (2026-08-24: emi_google_primary sat consented as the
    owner's Gmail for months because nothing told the human which account was being
    asked for, and nothing validated the answer).
    """
    from app.assistant.ServiceLocator.service_locator import DI

    return str(DI.env_registry.expected_email_for_account_id(account_id) or "").strip()


def _identity_mismatch(expected: str, actual: str | None) -> bool:
    """True only for a DEFINITE wrong-identity consent. An unclaimed account
    (expected == "") or an unfetchable principal (nest has no gmail scope) is not a
    mismatch — we never invent an expectation we don't have."""
    e = str(expected or "").strip().lower()
    a = str(actual or "").strip().lower()
    return bool(e) and bool(a) and e != a


def _google_auth_kwargs(account_id: str) -> dict:
    kwargs = {
        "access_type": "offline",
        "include_granted_scopes": "false",
        # select_account forces Google's account chooser so a multi-account
        # setup (e.g. emi_google_primary ≠ the user's Gmail) can pick the
        # RIGHT identity instead of silently re-consenting the logged-in one.
        "prompt": "select_account consent",
    }
    expected = _expected_identity(account_id)
    if expected:
        # Pre-select the intended identity in the chooser — the human no longer has
        # to guess which account a background re-auth prompt means.
        kwargs["login_hint"] = expected
    return kwargs


def _resolve_for_account(account_id: str, scope_set_hint: str | None = None):
    """Return (credentials_path, scopes, scope_set) for *account_id*.

    The account MUST be in the registry (configs/oauth_accounts.json). Unknown
    account_ids are rejected — no silent fallback to default credentials.
    *scope_set_hint* is ignored; scopes come from the registry entry.
    """
    if not oauth_registry.is_known_account(account_id):
        known = sorted(oauth_registry.list_accounts().keys())
        raise ValueError(
            f"Account '{account_id}' is not in the OAuth registry. "
            f"Add it to configs/oauth_accounts.json (with a client_credentials path "
            f"and scopes) before connecting. Known accounts: {known}."
        )
    cred_path = oauth_registry.get_client_credentials_path(account_id)
    scopes = oauth_registry.get_scopes(account_id)
    scope_set = oauth_registry.get_scope_set(account_id)
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
            authorization_url, state = flow.authorization_url(**_google_auth_kwargs(account_id))

        session['oauth_state'] = state
        session['oauth_account_id'] = account_id
        session['oauth_scope_set'] = scope_set

        redirect_after = request.args.get('redirect_to', 'features_settings')
        session['oauth_redirect_after'] = redirect_after

        expected_identity = _expected_identity(account_id)
        logger.info(
            "OAuth flow started: account_id=%s scope_set=%s cred_file=%s expected_identity=%s",
            account_id, scope_set, credentials_path.name, expected_identity or "(unclaimed)",
        )

        return jsonify({
            'success': True,
            'authorization_url': authorization_url,
            'account_id': account_id,
            # So the caller can TELL the human which identity to sign in as.
            'expected_identity': expected_identity,
        })

    except FileNotFoundError as e:
        # Credentials file missing — user-actionable, not a server bug.
        # Surface the full message (it includes the path + Google Cloud Console hint).
        logger.info("OAuth start blocked — credentials missing: %s", e)
        return jsonify({
            'success': False,
            'error': str(e),
            'reason': 'credentials_missing',
        }), 400
    except ValueError as e:
        # Unknown account_id (not in registry) — user-actionable.
        logger.info("OAuth start blocked — unregistered account: %s", e)
        return jsonify({
            'success': False,
            'error': str(e),
            'reason': 'account_not_registered',
        }), 400
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
            authorization_url, state = flow.authorization_url(**_google_auth_kwargs(account_id))

        session['oauth_state'] = state
        session['oauth_account_id'] = account_id
        session['oauth_scope_set'] = scope_set
        redirect_after = request.args.get('redirect_to', 'features_settings')
        session['oauth_redirect_after'] = redirect_after

        logger.info(
            "OAuth browser flow started: account_id=%s scope_set=%s cred_file=%s expected_identity=%s",
            account_id, scope_set, credentials_path.name,
            _expected_identity(account_id) or "(unclaimed)",
        )

        return redirect(authorization_url)
    except FileNotFoundError as e:
        # Credentials file missing — user-actionable.
        logger.info("OAuth browser flow blocked — credentials missing: %s", e)
        params = urlencode({
            'oauth_error': 'credentials_missing',
            'account_id': str(request.args.get('account_id') or '').strip(),
            'detail': str(e),
        })
        return redirect(f"{url_for('preferences.features_page')}?{params}")
    except ValueError as e:
        # Unknown account_id (not in registry) — user-actionable.
        logger.info("OAuth browser flow blocked — unregistered account: %s", e)
        params = urlencode({
            'oauth_error': 'account_not_registered',
            'account_id': str(request.args.get('account_id') or '').strip(),
            'detail': str(e),
        })
        return redirect(f"{url_for('preferences.features_page')}?{params}")
    except Exception as e:
        logger.error("Failed to start browser OAuth flow: %s", e)
        logger.debug("start_oauth_redirect exception details", exc_info=True)
        params = urlencode({
            'oauth_error': 'start_failed',
            'detail': str(e),
        })
        return redirect(f"{url_for('preferences.features_page')}?{params}")

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
        principal_email = fetch_principal_email(credentials)

        # IDENTITY GATE (2026-08-24): refuse to store a consent for the wrong Google
        # identity. Without this, signing in as the owner's Gmail when the assistant's
        # account was requested stored a working-but-wrong token: reads kept succeeding
        # (fetch paths don't assert identity), so the mis-consent stayed invisible until
        # the token expired and prompted again — "it asks and never takes". Only a
        # DEFINITE mismatch blocks; an unclaimed account or an unfetchable principal
        # (e.g. nest, no gmail scope) is stored as before.
        expected_identity = _expected_identity(account_id)
        if _identity_mismatch(expected_identity, principal_email):
            logger.error(
                "OAuth consent REFUSED — identity mismatch. account_id=%s consented_as=%s expected=%s",
                account_id, principal_email, expected_identity,
            )
            session.pop('oauth_state', None)
            session.pop('oauth_account_id', None)
            session.pop('oauth_scope_set', None)
            session.pop('oauth_redirect_after', None)
            params = urlencode({
                'oauth_error': 'identity_mismatch',
                'account_id': account_id,
                'detail': (
                    f"You signed in as {principal_email}, but this account must be "
                    f"{expected_identity}. Nothing was saved — retry and pick that account."
                ),
            })
            return redirect(url_for('preferences.features_page') + '?' + params)

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

