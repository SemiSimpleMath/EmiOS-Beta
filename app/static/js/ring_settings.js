// Ring Cameras settings

document.addEventListener('DOMContentLoaded', async function() {
    await loadRingConfig();
    bindRingActions();
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
