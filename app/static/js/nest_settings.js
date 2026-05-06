// Nest Thermostat settings

let oauthAccountConfig = {};
let oauthAccounts = [];

document.addEventListener('DOMContentLoaded', async function() {
    oauthAccountConfig = await loadOAuthAccountConfig();
    oauthAccounts = await loadOAuthAccounts();
    await loadNestConfig();
    renderNestOAuthStatus();
    bindNestActions();
    bindNestDevicesActions();
    await loadNestDevices();
});

async function loadNestConfig() {
    try {
        const cfg = await loadIntegrationConfig('nest');
        const el = (id) => document.getElementById(id);
        if (el('nest_enabled')) el('nest_enabled').checked = !!cfg.enabled;
        if (el('nest_endpoint_url')) el('nest_endpoint_url').value = String(cfg.endpoint_url || '');
        if (el('nest_token_env_var')) el('nest_token_env_var').value = String(cfg.token_env_var || '');
        if (el('nest_timeout_seconds')) el('nest_timeout_seconds').value = String(cfg.timeout_seconds || 20);
        const tokenStatus = el('nest_token_status');
        if (tokenStatus) {
            tokenStatus.textContent = cfg.token_is_set ? 'Token set' : 'Token not set';
            tokenStatus.className = `feature-status ${cfg.token_is_set ? 'enabled' : 'disabled'}`;
        }
        setResult('Nest config loaded.');
    } catch (error) {
        showError(`Failed loading Nest config: ${error.message}`);
    }
}

function renderNestOAuthStatus() {
    const host = document.getElementById('nest-oauth-status-container');
    if (!host) return;
    const nestAccountId = oauthAccountConfig.nest;
    const account = oauthAccounts.find(a => String(a.account_id || '').trim() === nestAccountId && a.is_active);
    if (account) {
        const principal = String(account.principal_email || 'unknown').trim();
        const updatedAt = account.updated_at ? new Date(account.updated_at).toLocaleString() : 'n/a';
        host.innerHTML = `
            <div class="feature-card">
                <div class="feature-info">
                    <p><strong>Status:</strong> <span class="feature-status enabled">Connected</span></p>
                    <p><strong>Account:</strong> ${escapeHtml(principal)} (<code>${escapeHtml(nestAccountId)}</code>)</p>
                    <p><strong>Updated:</strong> ${escapeHtml(updatedAt)}</p>
                    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;">
                        <button type="button" class="oauth-connect-btn" id="nest_oauth_reconnect_btn"><i class="fab fa-google"></i> Reconnect</button>
                        <button type="button" class="oauth-revoke-btn" id="nest_oauth_disconnect_btn"><i class="fas fa-unlink"></i> Disconnect</button>
                    </div>
                </div>
            </div>
        `;
    } else {
        host.innerHTML = `
            <div class="feature-card">
                <div class="feature-info">
                    <p><strong>Status:</strong> <span class="feature-status disabled">Not connected</span></p>
                    <button type="button" class="oauth-connect-btn" id="nest_oauth_connect_btn" style="margin-top:6px;">
                        <i class="fab fa-google"></i> Connect Google Nest Account
                    </button>
                </div>
            </div>
        `;
    }
    const connectBtn = document.getElementById('nest_oauth_connect_btn');
    if (connectBtn) connectBtn.addEventListener('click', () => handleGoogleOAuth(nestAccountId, 'nest', 'nest_settings'));
    const reconnectBtn = document.getElementById('nest_oauth_reconnect_btn');
    if (reconnectBtn) reconnectBtn.addEventListener('click', () => handleGoogleOAuth(nestAccountId, 'nest', 'nest_settings'));
    const disconnectBtn = document.getElementById('nest_oauth_disconnect_btn');
    if (disconnectBtn) disconnectBtn.addEventListener('click', () => revokeGoogleOAuth(nestAccountId, () => location.reload()));
}

function bindNestActions() {
    const bind = (id, handler) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('click', handler);
    };
    bind('nest_save_config_btn', async () => {
        try {
            await saveIntegrationConfig('nest', readNestConfigForm());
            showSuccess('Nest config saved.');
            await loadNestConfig();
        } catch (e) { showError(`Failed saving Nest config: ${e.message}`); }
    });
    bind('nest_refresh_status_btn', () => loadNestConfig());
    bind('nest_get_status_btn', () => runNest('get_status', {}));
    bind('nest_set_heat_btn', () => runNest('set_mode', { mode: 'heat' }));
    bind('nest_set_cool_btn', () => runNest('set_mode', { mode: 'cool' }));
    bind('nest_set_eco_on_btn', () => runNest('set_eco_mode', { enabled: true }));
    bind('nest_set_eco_off_btn', () => runNest('set_eco_mode', { enabled: false }));
    bind('nest_set_temp_btn', () => {
        const raw = String(document.getElementById('nest_temp_c_input')?.value || '').trim();
        if (!raw) { showError('Please enter target temperature.'); return; }
        runNest('set_target_temperature', { target_temperature_c: Number(raw) });
    });
}

function readNestConfigForm() {
    return {
        enabled: !!document.getElementById('nest_enabled')?.checked,
        endpoint_url: String(document.getElementById('nest_endpoint_url')?.value || '').trim(),
        token_env_var: String(document.getElementById('nest_token_env_var')?.value || '').trim(),
        timeout_seconds: parseInt(document.getElementById('nest_timeout_seconds')?.value || '20', 10),
    };
}

async function runNest(action, args) {
    try {
        const result = await runIntegrationAction('nest', action, args);
        setResult(JSON.stringify(result, null, 2));
        showSuccess(`Nest: ${action} succeeded`);
    } catch (e) {
        setResult(`ERROR: ${e.message}`);
        showError(`Nest ${action} failed: ${e.message}`);
    }
}

function setResult(text) {
    const el = document.getElementById('nest_action_result');
    if (el) el.textContent = text;
}

// ---------------------------------------------------------------------------
// Device Aliases — same pattern as ring/lights
// ---------------------------------------------------------------------------

function bindNestDevicesActions() {
    const bind = (id, h) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('click', h);
    };
    bind('nest_devices_discover_btn', discoverNestDevices);
    bind('nest_devices_save_btn', saveNestDevices);
    bind('nest_devices_reload_btn', loadNestDevices);
}

async function loadNestDevices() {
    try {
        const resp = await fetch('/api/integrations/nest/devices');
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'load failed');
        renderNestDevicesTable(data.devices || []);
    } catch (e) {
        showError(`Failed loading saved devices: ${e.message}`);
    }
}

async function discoverNestDevices() {
    try {
        const result = await runIntegrationAction('nest', 'get_status', {});
        // _nest_get_status (no device_id) returns {thermostats: [{name, type, mode, ...}]}
        let therms = [];
        if (Array.isArray(result?.thermostats)) therms = result.thermostats;
        else if (Array.isArray(result?.result?.thermostats)) therms = result.result.thermostats;
        if (!therms.length) {
            showError('No thermostats returned from Nest.');
            return;
        }
        // SDM "name" is a long URI like "enterprises/.../devices/AVPH...".
        // Default alias = last URI segment ("AVPH..."), user typically renames.
        const devices = therms.map(t => {
            const fullName = String(t.name || '').trim();
            const tail = fullName.split('/').filter(Boolean).pop() || fullName || 'Thermostat';
            return {
                alias: tail,
                device_id: fullName,
                notes: String(t.type || '').replace(/^sdm\.devices\.types\./, ''),
            };
        });
        renderNestDevicesTable(devices);
        showSuccess(`Discovered ${devices.length} thermostat(s). Edit aliases then click Save.`);
    } catch (e) {
        showError(`Discovery failed: ${e.message}`);
    }
}

async function saveNestDevices() {
    try {
        const devices = readNestDevicesTable();
        const resp = await fetch('/api/integrations/nest/devices', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ devices }),
        });
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'save failed');
        renderNestDevicesTable(data.devices || []);
        showSuccess(`Saved ${data.devices.length} device(s). Restart Flask for the planner to see them.`);
    } catch (e) {
        showError(`Save failed: ${e.message}`);
    }
}

function renderNestDevicesTable(devices) {
    const tbody = document.getElementById('nest_devices_tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!devices.length) {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan="4" style="padding:14px;color:#6b7280;font-style:italic;">No devices configured. Click "Discover from Nest" to auto-populate.</td>';
        tbody.appendChild(tr);
        return;
    }
    devices.forEach(d => tbody.appendChild(buildNestDeviceRow(d)));
}

function buildNestDeviceRow(device) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #e5e7eb';
    const aliasInput = `<input type="text" class="nest-device-alias" value="${escapeAttrN(device.alias || '')}" style="width:100%;padding:6px;">`;
    const idInput = `<input type="text" class="nest-device-id" value="${escapeAttrN(device.device_id || '')}" style="width:100%;padding:6px;font-family:monospace;font-size:11px;">`;
    const notesInput = `<input type="text" class="nest-device-notes" value="${escapeAttrN(device.notes || '')}" style="width:100%;padding:6px;font-size:12px;">`;
    tr.innerHTML =
        `<td style="padding:6px;">${aliasInput}</td>` +
        `<td style="padding:6px;">${idInput}</td>` +
        `<td style="padding:6px;">${notesInput}</td>` +
        `<td style="padding:6px;text-align:center;"><button type="button" class="nest-device-remove" style="background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;border-radius:4px;padding:4px 8px;cursor:pointer;">×</button></td>`;
    tr.querySelector('.nest-device-remove').addEventListener('click', () => tr.remove());
    return tr;
}

function readNestDevicesTable() {
    const rows = document.querySelectorAll('#nest_devices_tbody tr');
    const out = [];
    rows.forEach(tr => {
        const alias = tr.querySelector('.nest-device-alias')?.value?.trim() || '';
        const device_id = tr.querySelector('.nest-device-id')?.value?.trim() || '';
        const notes = tr.querySelector('.nest-device-notes')?.value?.trim() || '';
        if (alias && device_id) {
            const d = { alias, device_id };
            if (notes) d.notes = notes;
            out.push(d);
        }
    });
    return out;
}

function escapeAttrN(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
