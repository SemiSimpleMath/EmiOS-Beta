// Additional Google Accounts settings

let oauthAccountConfig = {};
let oauthAccounts = [];

document.addEventListener('DOMContentLoaded', async function() {
    oauthAccountConfig = await loadOAuthAccountConfig();
    oauthAccounts = await loadOAuthAccounts();
    renderAdditionalAccounts();
    bindConnectButton();

    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('oauth_success') === 'true') {
        showSuccess('Google account connected successfully!');
        window.history.replaceState({}, document.title, window.location.pathname);
        setTimeout(() => location.reload(), 1500);
    }
});

function bindConnectButton() {
    const btn = document.getElementById('connect-additional-btn');
    if (!btn) return;
    btn.addEventListener('click', () => {
        const input = document.getElementById('additional-oauth-account-id');
        const accountId = String(input?.value || '').trim();
        if (!accountId) { showError('Please enter an account_id.'); return; }
        if (accountId === oauthAccountConfig.default) { showError('That account_id is reserved for the primary user.'); return; }
        handleGoogleOAuth(accountId, 'workspace', 'google_accounts_settings');
    });
}

function renderAdditionalAccounts() {
    const host = document.getElementById('additional-accounts-list');
    if (!host) return;
    const additional = oauthAccounts.filter(a => String(a.account_id || '').trim() !== oauthAccountConfig.default);
    if (!additional.length) {
        host.innerHTML = '<p class="feature-status disabled"><i class="fas fa-info-circle"></i> No additional accounts connected.</p>';
        return;
    }
    host.innerHTML = additional.map(account => {
        const accountId = String(account.account_id || '').trim();
        const principal = String(account.principal_email || 'unknown').trim();
        const status = account.is_active ? 'Active' : 'Revoked';
        const statusClass = account.is_active ? 'enabled' : 'disabled';
        const updatedAt = account.updated_at ? new Date(account.updated_at).toLocaleString() : 'n/a';
        const scopes = Array.isArray(account.granted_scopes) ? account.granted_scopes : [];
        return `
            <div class="feature-card" style="margin-bottom:10px;">
                <div class="feature-info">
                    <h3><i class="fab fa-google"></i> ${escapeHtml(accountId)}</h3>
                    <p><strong>Principal:</strong> ${escapeHtml(principal)}</p>
                    <p><strong>Status:</strong> <span class="feature-status ${statusClass}">${escapeHtml(status)}</span></p>
                    <p><strong>Updated:</strong> ${escapeHtml(updatedAt)}</p>
                    <p><strong>Scopes:</strong> ${escapeHtml(scopes.join(', ') || 'none')}</p>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
                        <button type="button" class="oauth-connect-btn" data-account-id="${escapeHtmlAttr(accountId)}">
                            <i class="fab fa-google"></i> Reconnect
                        </button>
                        <button type="button" class="oauth-revoke-btn" data-account-id="${escapeHtmlAttr(accountId)}">
                            <i class="fas fa-unlink"></i> Revoke
                        </button>
                    </div>
                </div>
            </div>
        `;
    }).join('');
    host.querySelectorAll('.oauth-connect-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const accountId = String(btn.getAttribute('data-account-id') || '').trim();
            if (accountId) handleGoogleOAuth(accountId, 'workspace', 'google_accounts_settings');
        });
    });
    host.querySelectorAll('.oauth-revoke-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const accountId = String(btn.getAttribute('data-account-id') || '').trim();
            if (accountId) revokeGoogleOAuth(accountId, () => location.reload());
        });
    });
}
