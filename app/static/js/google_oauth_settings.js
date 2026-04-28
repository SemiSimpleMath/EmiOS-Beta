// Google OAuth Settings — primary account + service routing

let oauthAccountConfig = {};
let oauthAccounts = [];

document.addEventListener('DOMContentLoaded', async function() {
    oauthAccountConfig = await loadOAuthAccountConfig();
    oauthAccounts = await loadOAuthAccounts();
    renderPrimaryAccount();
    renderServiceMappings();

    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('oauth_success') === 'true') {
        showSuccess('Google account connected successfully!');
        window.history.replaceState({}, document.title, window.location.pathname);
        setTimeout(() => location.reload(), 1500);
    }
});

function renderPrimaryAccount() {
    const host = document.getElementById('google-oauth-primary-account');
    if (!host) return;
    const primary = oauthAccounts.find(a => String(a.account_id || '').trim() === oauthAccountConfig.default);
    if (!primary) {
        host.innerHTML = `
            <div class="feature-card">
                <div class="feature-info">
                    <h3><i class="fab fa-google"></i> ${escapeHtml(oauthAccountConfig.default)}</h3>
                    <p><strong>Status:</strong> <span class="feature-status disabled">Not connected</span></p>
                    <button type="button" class="oauth-connect-btn" id="connect-primary-btn">
                        <i class="fab fa-google"></i> Connect Primary Account
                    </button>
                </div>
            </div>
        `;
        const btn = document.getElementById('connect-primary-btn');
        if (btn) btn.addEventListener('click', () => handleGoogleOAuth(oauthAccountConfig.default, 'workspace', 'google_oauth_settings'));
    } else {
        const principal = String(primary.principal_email || 'unknown').trim();
        const updatedAt = primary.updated_at ? new Date(primary.updated_at).toLocaleString() : 'n/a';
        const scopes = Array.isArray(primary.granted_scopes) ? primary.granted_scopes : [];
        host.innerHTML = `
            <div class="feature-card">
                <div class="feature-info">
                    <h3><i class="fab fa-google"></i> ${escapeHtml(oauthAccountConfig.default)}</h3>
                    <p><strong>Principal:</strong> ${escapeHtml(principal)}</p>
                    <p><strong>Status:</strong> <span class="feature-status enabled">Active</span></p>
                    <p><strong>Updated:</strong> ${escapeHtml(updatedAt)}</p>
                    <p><strong>Scopes:</strong> ${escapeHtml(scopes.join(', ') || 'none')}</p>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
                        <button type="button" class="oauth-connect-btn" id="reconnect-primary-btn">
                            <i class="fab fa-google"></i> Reconnect
                        </button>
                        <button type="button" class="oauth-revoke-btn" id="revoke-primary-btn">
                            <i class="fas fa-unlink"></i> Revoke
                        </button>
                    </div>
                </div>
            </div>
        `;
        const reconnectBtn = document.getElementById('reconnect-primary-btn');
        if (reconnectBtn) reconnectBtn.addEventListener('click', () => handleGoogleOAuth(oauthAccountConfig.default, 'workspace', 'google_oauth_settings'));
        const revokeBtn = document.getElementById('revoke-primary-btn');
        if (revokeBtn) revokeBtn.addEventListener('click', () => revokeGoogleOAuth(oauthAccountConfig.default, () => location.reload()));
    }
}

function renderServiceMappings() {
    const host = document.getElementById('google-oauth-service-grid');
    if (!host) return;
    const services = [
        { key: 'gmail', icon: 'fas fa-envelope', label: 'Gmail' },
        { key: 'calendar', icon: 'fas fa-calendar', label: 'Calendar' },
        { key: 'tasks', icon: 'fas fa-tasks', label: 'Tasks' },
    ];
    host.innerHTML = services.map(svc => {
        const accountId = oauthAccountConfig[svc.key] || oauthAccountConfig.default;
        const account = oauthAccounts.find(a => String(a.account_id || '').trim() === accountId && a.is_active);
        const statusHtml = account
            ? '<span class="feature-status enabled">Active</span>'
            : '<span class="feature-status disabled">Not connected</span>';
        return `
            <div class="feature-card">
                <div class="feature-info">
                    <h3><i class="${svc.icon}"></i> ${svc.label}</h3>
                    <p>Account: <code>${escapeHtml(accountId)}</code></p>
                    <p><strong>Status:</strong> ${statusHtml}</p>
                    <button type="button" class="oauth-connect-btn" data-account-id="${escapeHtmlAttr(accountId)}">
                        <i class="fab fa-google"></i> Connect ${svc.label} Account
                    </button>
                </div>
            </div>
        `;
    }).join('');
    host.querySelectorAll('.oauth-connect-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const accountId = String(btn.getAttribute('data-account-id') || '').trim();
            if (accountId) handleGoogleOAuth(accountId, 'workspace', 'google_oauth_settings');
        });
    });
}
