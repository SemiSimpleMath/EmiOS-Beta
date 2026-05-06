// Ring Cameras settings

document.addEventListener('DOMContentLoaded', async function() {
    await loadRingConfig();
    bindRingActions();
    bindRingDevicesActions();
    await loadRingDevices();
});

async function loadRingConfig() {
    try {
        const cfg = await loadIntegrationConfig('ring');
        const el = (id) => document.getElementById(id);
        if (el('ring_enabled')) el('ring_enabled').checked = !!cfg.enabled;
        if (el('ring_endpoint_url')) el('ring_endpoint_url').value = String(cfg.endpoint_url || '');
        if (el('ring_token_env_var')) el('ring_token_env_var').value = String(cfg.token_env_var || '');
        if (el('ring_timeout_seconds')) el('ring_timeout_seconds').value = String(cfg.timeout_seconds || 20);
        const tokenStatus = el('ring_token_status');
        if (tokenStatus) {
            tokenStatus.textContent = cfg.token_is_set ? 'Token set' : 'Token not set';
            tokenStatus.className = `feature-status ${cfg.token_is_set ? 'enabled' : 'disabled'}`;
        }
        setResult('Ring config loaded.');
    } catch (error) {
        showError(`Failed loading Ring config: ${error.message}`);
    }
}

function bindRingActions() {
    const bind = (id, handler) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('click', handler);
    };
    bind('ring_save_config_btn', async () => {
        try {
            await saveIntegrationConfig('ring', readRingConfigForm());
            showSuccess('Ring config saved.');
            await loadRingConfig();
        } catch (e) { showError(`Failed saving Ring config: ${e.message}`); }
    });
    bind('ring_refresh_status_btn', () => loadRingConfig());
    bind('ring_list_btn', () => runRing('list_cameras', {}));
    bind('ring_snapshot_btn', () => {
        const id = getCameraId(); if (!id) return;
        runRing('get_snapshot', { camera_id: id });
    });
    bind('ring_events_btn', () => {
        const id = getCameraId(); if (!id) return;
        runRing('get_recent_events', { camera_id: id, lookback_minutes: 60 });
    });
    bind('ring_siren_on_btn', () => {
        const id = getCameraId(); if (!id) return;
        runRing('set_siren', { camera_id: id, enabled: true });
    });
    bind('ring_siren_off_btn', () => {
        const id = getCameraId(); if (!id) return;
        runRing('set_siren', { camera_id: id, enabled: false });
    });
}

function getCameraId() {
    const id = String(document.getElementById('ring_camera_id_input')?.value || '').trim();
    if (!id) { showError('Camera ID is required.'); return null; }
    return id;
}

function readRingConfigForm() {
    return {
        enabled: !!document.getElementById('ring_enabled')?.checked,
        endpoint_url: String(document.getElementById('ring_endpoint_url')?.value || '').trim(),
        token_env_var: String(document.getElementById('ring_token_env_var')?.value || '').trim(),
        timeout_seconds: parseInt(document.getElementById('ring_timeout_seconds')?.value || '20', 10),
    };
}

async function runRing(action, args) {
    try {
        const result = await runIntegrationAction('ring', action, args);
        setResult(JSON.stringify(result, null, 2));
        showSuccess(`Ring: ${action} succeeded`);
    } catch (e) {
        setResult(`ERROR: ${e.message}`);
        showError(`Ring ${action} failed: ${e.message}`);
    }
}

function setResult(text) {
    const el = document.getElementById('ring_action_result');
    if (el) el.textContent = text;
}

// ---------------------------------------------------------------------------
// Device Aliases
// ---------------------------------------------------------------------------

function bindRingDevicesActions() {
    const bind = (id, h) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('click', h);
    };
    bind('ring_devices_discover_btn', discoverRingDevices);
    bind('ring_devices_save_btn', saveRingDevices);
    bind('ring_devices_reload_btn', loadRingDevices);
}

async function loadRingDevices() {
    try {
        const resp = await fetch('/api/integrations/ring/devices');
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'load failed');
        renderRingDevicesTable(data.devices || []);
    } catch (e) {
        showError(`Failed loading saved devices: ${e.message}`);
    }
}

async function discoverRingDevices() {
    try {
        const result = await runIntegrationAction('ring', 'list_cameras', {});
        // result.result.cameras for the bridge response shape, or result.cameras
        // depending on whether the gateway un-wraps. Defensive parse.
        let cams = [];
        if (Array.isArray(result?.cameras)) {
            cams = result.cameras;
        } else if (Array.isArray(result?.result?.cameras)) {
            cams = result.result.cameras;
        }
        if (!cams.length) {
            showError('No cameras returned from Ring.');
            return;
        }
        // Map Ring camera info → device entry. Use Ring's name as the default alias.
        const devices = cams.map(c => ({
            alias: String(c.name || '').trim() || `Camera ${c.id}`,
            camera_id: String(c.id || '').trim(),
            notes: [c.model, c.kind].filter(Boolean).join(' / '),
        }));
        renderRingDevicesTable(devices);
        showSuccess(`Discovered ${devices.length} camera(s). Edit aliases then click Save.`);
    } catch (e) {
        showError(`Discovery failed: ${e.message}`);
    }
}

async function saveRingDevices() {
    try {
        const devices = readRingDevicesTable();
        const resp = await fetch('/api/integrations/ring/devices', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ devices }),
        });
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || 'save failed');
        renderRingDevicesTable(data.devices || []);
        showSuccess(`Saved ${data.devices.length} device(s). Restart Flask for the planner to see them.`);
    } catch (e) {
        showError(`Save failed: ${e.message}`);
    }
}

function renderRingDevicesTable(devices) {
    const tbody = document.getElementById('ring_devices_tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!devices.length) {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td colspan="4" style="padding:14px;color:#6b7280;font-style:italic;">No devices configured. Click "Discover from Ring" to auto-populate.</td>';
        tbody.appendChild(tr);
        return;
    }
    devices.forEach(d => tbody.appendChild(buildRingDeviceRow(d)));
}

function buildRingDeviceRow(device) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #e5e7eb';
    const aliasInput = `<input type="text" class="ring-device-alias" value="${escapeAttr(device.alias || '')}" style="width:100%;padding:6px;">`;
    const camInput = `<input type="text" class="ring-device-camid" value="${escapeAttr(device.camera_id || '')}" style="width:100%;padding:6px;font-family:monospace;font-size:12px;">`;
    const notesInput = `<input type="text" class="ring-device-notes" value="${escapeAttr(device.notes || '')}" style="width:100%;padding:6px;font-size:12px;">`;
    tr.innerHTML =
        `<td style="padding:6px;">${aliasInput}</td>` +
        `<td style="padding:6px;">${camInput}</td>` +
        `<td style="padding:6px;">${notesInput}</td>` +
        `<td style="padding:6px;text-align:center;"><button type="button" class="ring-device-remove" style="background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;border-radius:4px;padding:4px 8px;cursor:pointer;">×</button></td>`;
    tr.querySelector('.ring-device-remove').addEventListener('click', () => tr.remove());
    return tr;
}

function readRingDevicesTable() {
    const rows = document.querySelectorAll('#ring_devices_tbody tr');
    const out = [];
    rows.forEach(tr => {
        const alias = tr.querySelector('.ring-device-alias')?.value?.trim() || '';
        const cam_id = tr.querySelector('.ring-device-camid')?.value?.trim() || '';
        const notes = tr.querySelector('.ring-device-notes')?.value?.trim() || '';
        if (alias && cam_id) {
            const d = { alias, camera_id: cam_id };
            if (notes) d.notes = notes;
            out.push(d);
        }
    });
    return out;
}

function escapeAttr(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
