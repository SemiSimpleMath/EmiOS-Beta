// Shared helpers for integration settings pages

function showError(message) {
    const el = document.getElementById('error-message');
    if (!el) return;
    el.textContent = message;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 5000);
}

function showSuccess(message) {
    const el = document.getElementById('success-message');
    if (!el) return;
    el.textContent = message;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function escapeHtml(value) {
    const s = String(value == null ? '' : value);
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function escapeHtmlAttr(value) {
    return escapeHtml(value).replace(/'/g, '&#39;');
}

async function handleGoogleOAuth(accountId, scopeSet, redirectPage) {
    const normalizedAccountId = String(accountId || '').trim() || 'google_user_primary';
    const scopeParam = String(scopeSet || 'workspace').trim();
    const redirect = String(redirectPage || 'google_oauth_settings').trim();
    try {
        const url = `/api/oauth/google/start?redirect_to=${encodeURIComponent(redirect)}&account_id=${encodeURIComponent(normalizedAccountId)}&scope_set=${encodeURIComponent(scopeParam)}`;
        const response = await fetch(url);
        const data = await response.json();
        if (data.success && data.authorization_url) {
            window.location.href = data.authorization_url;
        } else {
            showError('Failed to start OAuth: ' + (data.error || 'Unknown error'));
        }
    } catch (error) {
        console.error('OAuth error:', error);
        showError('Failed to start OAuth: ' + error.message);
    }
}

async function revokeGoogleOAuth(accountId, onSuccess) {
    const normalizedAccountId = String(accountId || '').trim();
    if (!normalizedAccountId) { showError('Missing account_id for revoke.'); return; }
    try {
        const response = await fetch('/api/oauth/google/revoke', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ account_id: normalizedAccountId })
        });
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Failed to revoke OAuth account');
        showSuccess(`Revoked Google OAuth account: ${normalizedAccountId}`);
        if (typeof onSuccess === 'function') onSuccess();
    } catch (error) {
        console.error('OAuth revoke error:', error);
        showError('Failed to revoke OAuth: ' + error.message);
    }
}

async function loadOAuthAccountConfig() {
    let config = {
        default: 'google_user_primary',
        gmail: 'google_user_primary',
        calendar: 'google_user_primary',
        tasks: 'google_user_primary',
        nest: 'google_nest'
    };
    try {
        const response = await fetch('/api/oauth/google/config');
        const data = await response.json();
        if (data.success && data.accounts) {
            config = {
                default: String(data.accounts.default || 'google_user_primary').trim() || 'google_user_primary',
                gmail: String(data.accounts.gmail || data.accounts.default || 'google_user_primary').trim() || 'google_user_primary',
                calendar: String(data.accounts.calendar || data.accounts.default || 'google_user_primary').trim() || 'google_user_primary',
                tasks: String(data.accounts.tasks || data.accounts.default || 'google_user_primary').trim() || 'google_user_primary',
                nest: String(data.accounts.nest || 'google_nest').trim() || 'google_nest'
            };
        }
    } catch (error) {
        console.error('Failed to load OAuth account config:', error);
    }
    return config;
}

async function loadOAuthAccounts() {
    try {
        const response = await fetch('/api/oauth/google/accounts');
        const data = await response.json();
        if (!data.success) throw new Error(data.error || 'Failed to load OAuth accounts');
        return Array.isArray(data.accounts) ? data.accounts : [];
    } catch (error) {
        console.error('Failed to load OAuth account list:', error);
        showError(`Failed to load OAuth account list: ${String(error.message || error)}`);
        return [];
    }
}

// Smart home shared helpers
async function loadIntegrationConfig(integrationKey) {
    const response = await fetch(`/api/integrations/${integrationKey}/config`);
    const data = await response.json();
    if (!data.success || !data.config) {
        throw new Error(data.error || `Failed to load ${integrationKey} integration config.`);
    }
    return data.config;
}

async function saveIntegrationConfig(integrationKey, payload) {
    const response = await fetch(`/api/integrations/${integrationKey}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
    });
    const data = await response.json();
    if (!data.success) throw new Error(data.error || `Failed to save ${integrationKey} config.`);
}

async function runIntegrationAction(integrationKey, action, argumentsObj) {
    const response = await fetch(`/api/integrations/${integrationKey}/action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, arguments: argumentsObj || {} })
    });
    const data = await response.json();
    if (!data.success) throw new Error(data.error || `${integrationKey} action failed: ${action}`);
    return data.data || {};
}
