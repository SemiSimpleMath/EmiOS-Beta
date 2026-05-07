document.addEventListener("DOMContentLoaded", function () {
    bindLightsUiActions();
    loadLightsIntegrationUi();
    bindLightsToolDescriptionActions();
    loadLightsToolDescription();
    bindLightsDevicesActions();
    loadLightsDevices();
});

async function loadLightsToolDescription() {
    try {
        const response = await fetch("/api/integrations/lights/tool-description");
        const data = await response.json();
        if (!data.success) {
            throw new Error(data.error || "Failed to load tool description.");
        }
        const el = document.getElementById("lights_tool_description");
        if (el) el.value = String(data.content || "");
    } catch (error) {
        console.error("Failed loading lights tool description:", error);
        showError(`Failed loading lights tool description: ${String(error.message || error)}`);
    }
}

async function saveLightsToolDescription() {
    const el = document.getElementById("lights_tool_description");
    const content = el ? el.value : "";
    const response = await fetch("/api/integrations/lights/tool-description", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: content }),
    });
    const data = await response.json();
    if (!data.success) {
        throw new Error(data.error || "Failed to save tool description.");
    }
}

function bindLightsToolDescriptionActions() {
    bindOnce("lights_tool_description_save_btn", async () => {
        try {
            await saveLightsToolDescription();
            showSuccess("Tool description saved. Restart Flask for agents to pick it up.");
        } catch (error) {
            showError(`Failed saving tool description: ${String(error.message || error)}`);
        }
    });
    bindOnce("lights_tool_description_refresh_btn", async () => {
        try {
            await loadLightsToolDescription();
            showSuccess("Tool description reloaded from disk.");
        } catch (error) {
            showError(`Failed reloading: ${String(error.message || error)}`);
        }
    });
}

async function loadLightsIntegrationUi() {
    try {
        const response = await fetch("/api/integrations/lights/config");
        const data = await response.json();
        if (!data.success || !data.config) {
            throw new Error(data.error || "Failed to load lights integration config.");
        }
        const cfg = data.config;
        const hosts = Array.isArray(cfg.kasa_device_hosts) ? cfg.kasa_device_hosts : [];
        document.getElementById("lights_enabled").checked = !!cfg.enabled;
        document.getElementById("lights_endpoint_url").value = String(cfg.endpoint_url || "");
        document.getElementById("lights_token_env_var").value = String(cfg.token_env_var || "");
        document.getElementById("lights_timeout_seconds").value = String(cfg.timeout_seconds || 20);
        document.getElementById("lights_kasa_discovery_timeout_seconds").value = String(
            cfg.kasa_discovery_timeout_seconds || 4
        );
        document.getElementById("lights_kasa_hosts").value = hosts.join("\n");

        const tokenStatus = document.getElementById("lights_token_status");
        tokenStatus.textContent = cfg.token_is_set ? "Token set" : "Token not set";
        tokenStatus.className = `feature-status ${cfg.token_is_set ? "enabled" : "disabled"}`;
        setResult("Config loaded.");
    } catch (error) {
        console.error("Failed loading lights integration:", error);
        showError(`Failed loading lights integration: ${String(error.message || error)}`);
    }
}

function bindLightsUiActions() {
    bindOnce("lights_save_config_btn", async () => {
        try {
            await saveLightsConfig();
            showSuccess("Lights config saved.");
            await loadLightsIntegrationUi();
        } catch (error) {
            console.error("Failed saving lights config:", error);
            showError(`Failed saving lights config: ${String(error.message || error)}`);
        }
    });

    bindOnce("lights_refresh_config_btn", async () => {
        await loadLightsIntegrationUi();
    });

    bindOnce("lights_discover_hosts_btn", async () => {
        try {
            const payload = await runLightsAction("discover_hosts", {});
            const hosts = Array.isArray(payload?.result?.hosts)
                ? payload.result.hosts
                : Array.isArray(payload?.hosts)
                    ? payload.hosts
                    : [];
            if (!hosts.length) {
                showError("No Kasa hosts discovered.");
            } else {
                document.getElementById("lights_kasa_hosts").value = hosts.join("\n");
                showSuccess(`Discovered ${hosts.length} host(s).`);
            }
            setResult(JSON.stringify(payload, null, 2));
        } catch (error) {
            console.error("Failed discovering hosts:", error);
            showError(`Discover failed: ${String(error.message || error)}`);
        }
    });

    bindOnce("lights_list_btn", async () => {
        await runLightsActionWithUi("list_lights", {});
    });

    bindOnce("lights_on_btn", async () => {
        const room = String(document.getElementById("lights_room_input")?.value || "").trim();
        await runLightsActionWithUi("set_light_power", { state: "on", room });
    });

    bindOnce("lights_off_btn", async () => {
        const room = String(document.getElementById("lights_room_input")?.value || "").trim();
        await runLightsActionWithUi("set_light_power", { state: "off", room });
    });

    bindOnce("lights_brightness_btn", async () => {
        const room = String(document.getElementById("lights_room_input")?.value || "").trim();
        const raw = String(document.getElementById("lights_brightness_input")?.value || "").trim();
        const brightness = Number.parseInt(raw, 10);
        if (!Number.isFinite(brightness) || brightness < 0 || brightness > 100) {
            showError("Brightness must be an integer from 0 to 100.");
            return;
        }
        await runLightsActionWithUi("set_light_brightness", { brightness_pct: brightness, room });
    });
}

function bindOnce(id, handler) {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.dataset.bound === "1") return;
    el.dataset.bound = "1";
    el.addEventListener("click", handler);
}

async function saveLightsConfig() {
    const endpoint_url = String(document.getElementById("lights_endpoint_url")?.value || "").trim();
    const token_env_var = String(document.getElementById("lights_token_env_var")?.value || "").trim();
    const timeout_seconds = Number.parseInt(
        String(document.getElementById("lights_timeout_seconds")?.value || "").trim(),
        10
    );
    const kasa_discovery_timeout_seconds = Number.parseInt(
        String(document.getElementById("lights_kasa_discovery_timeout_seconds")?.value || "").trim(),
        10
    );
    const enabled = !!document.getElementById("lights_enabled")?.checked;
    const hosts = String(document.getElementById("lights_kasa_hosts")?.value || "")
        .split(/\r?\n/)
        .map((x) => String(x || "").trim())
        .filter((x) => !!x);

    if (!endpoint_url) throw new Error("endpoint_url is required.");
    if (!token_env_var) throw new Error("token_env_var is required.");
    if (!Number.isFinite(timeout_seconds) || timeout_seconds <= 0) {
        throw new Error("timeout_seconds must be a positive integer.");
    }
    if (!Number.isFinite(kasa_discovery_timeout_seconds) || kasa_discovery_timeout_seconds <= 0) {
        throw new Error("kasa_discovery_timeout_seconds must be a positive integer.");
    }

    const response = await fetch("/api/integrations/lights/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            enabled,
            endpoint_url,
            token_env_var,
            timeout_seconds,
            kasa_device_hosts: hosts,
            kasa_discovery_timeout_seconds,
        }),
    });
    const data = await response.json();
    if (!data.success) {
        throw new Error(data.error || "Failed to save lights config.");
    }
}

async function runLightsAction(command, argumentsObj) {
    const hostsFromUi = getHostsFromUi();
    const payloadArgs = {
        ...(argumentsObj || {}),
    };
    if (hostsFromUi.length > 0) {
        payloadArgs.kasa_device_hosts = hostsFromUi;
    }

    const response = await fetch("/api/integrations/lights/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            command,
            arguments: payloadArgs,
        }),
    });
    const data = await response.json();
    if (!data.success) {
        throw new Error(data.error || `Lights command failed: ${command}`);
    }
    return data.data || {};
}

function getHostsFromUi() {
    return String(document.getElementById("lights_kasa_hosts")?.value || "")
        .split(/\r?\n/)
        .map((x) => String(x || "").trim())
        .filter((x) => !!x);
}

async function runLightsActionWithUi(action, argumentsObj) {
    try {
        const payload = await runLightsAction(action, argumentsObj);
        setResult(JSON.stringify(payload, null, 2));
        showSuccess(`Lights action succeeded: ${action}`);
    } catch (error) {
        console.error(`Lights action failed (${action}):`, error);
        setResult(`ERROR: ${String(error.message || error)}`);
        showError(`Lights action failed: ${String(error.message || error)}`);
    }
}

function setResult(text) {
    const el = document.getElementById("lights_action_result");
    if (!el) return;
    el.textContent = String(text || "");
}

function showError(message) {
    const el = document.getElementById("error-message");
    if (!el) return;
    el.textContent = String(message || "Unknown error");
    el.style.display = "block";
    setTimeout(() => {
        el.style.display = "none";
    }, 6000);
}

function showSuccess(message) {
    const el = document.getElementById("success-message");
    if (!el) return;
    el.textContent = String(message || "");
    el.style.display = "block";
    setTimeout(() => {
        el.style.display = "none";
    }, 3000);
}

// ---------------------------------------------------------------------------
// Device Aliases (Kasa) — same pattern as ring_settings.js
// ---------------------------------------------------------------------------

function bindLightsDevicesActions() {
    const bind = (id, h) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("click", h);
    };
    bind("lights_devices_discover_btn", discoverLightsDevices);
    bind("lights_devices_save_btn", saveLightsDevices);
    bind("lights_devices_reload_btn", loadLightsDevices);
}

async function loadLightsDevices() {
    try {
        const resp = await fetch("/api/integrations/lights/devices");
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || "load failed");
        renderLightsDevicesTable(data.devices || []);
    } catch (e) {
        showError(`Failed loading saved devices: ${e.message}`);
    }
}

async function discoverLightsDevices() {
    try {
        // scan_lan triggers the dedicated LAN-scan path (_kasa_scan_lan),
        // the only place that's permitted to broadcast-discover. The
        // list_lights / set_light_power runtime ops no longer fall back
        // to LAN scan — they read configured devices only — so the
        // discover button MUST use scan_lan, not list_lights.
        const result = await runLightsAction("scan_lan", {});
        // _kasa_discover_hosts returns {hosts, lights}.
        let lights = [];
        if (Array.isArray(result?.lights)) lights = result.lights;
        else if (Array.isArray(result?.result?.lights)) lights = result.result.lights;
        if (!lights.length) {
            showError("No Kasa devices found on the LAN. Make sure your lights are powered on and reachable from this machine.");
            return;
        }
        const devices = lights.map(d => ({
            alias: String(d.alias || "").trim() || `Light ${d.host || ""}`,
            host: String(d.host || "").trim(),
            notes: [d.model, d.device_type].filter(Boolean).join(" / "),
        }));
        renderLightsDevicesTable(devices);
        showSuccess(`Discovered ${devices.length} device(s). Edit aliases then click Save.`);
    } catch (e) {
        showError(`Discovery failed: ${e.message}`);
    }
}

async function saveLightsDevices() {
    try {
        const devices = readLightsDevicesTable();
        const resp = await fetch("/api/integrations/lights/devices", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ devices }),
        });
        const data = await resp.json();
        if (!data.success) throw new Error(data.error || "save failed");
        renderLightsDevicesTable(data.devices || []);
        showSuccess(`Saved ${data.devices.length} device(s). Restart Flask for the planner to see them.`);
    } catch (e) {
        showError(`Save failed: ${e.message}`);
    }
}

function renderLightsDevicesTable(devices) {
    const tbody = document.getElementById("lights_devices_tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    if (!devices.length) {
        const tr = document.createElement("tr");
        tr.innerHTML = '<td colspan="4" style="padding:14px;color:#6b7280;font-style:italic;">No devices configured. Click "Discover from Kasa" to auto-populate.</td>';
        tbody.appendChild(tr);
        return;
    }
    devices.forEach(d => tbody.appendChild(buildLightsDeviceRow(d)));
}

function buildLightsDeviceRow(device) {
    const tr = document.createElement("tr");
    tr.style.borderBottom = "1px solid #e5e7eb";
    const aliasInput = `<input type="text" class="lights-device-alias" value="${escapeAttrL(device.alias || '')}" style="width:100%;padding:6px;">`;
    const hostInput = `<input type="text" class="lights-device-host" value="${escapeAttrL(device.host || '')}" style="width:100%;padding:6px;font-family:monospace;font-size:12px;">`;
    const notesInput = `<input type="text" class="lights-device-notes" value="${escapeAttrL(device.notes || '')}" style="width:100%;padding:6px;font-size:12px;">`;
    tr.innerHTML =
        `<td style="padding:6px;">${aliasInput}</td>` +
        `<td style="padding:6px;">${hostInput}</td>` +
        `<td style="padding:6px;">${notesInput}</td>` +
        `<td style="padding:6px;text-align:center;"><button type="button" class="lights-device-remove" style="background:#fee2e2;color:#991b1b;border:1px solid #fca5a5;border-radius:4px;padding:4px 8px;cursor:pointer;">×</button></td>`;
    tr.querySelector(".lights-device-remove").addEventListener("click", () => tr.remove());
    return tr;
}

function readLightsDevicesTable() {
    const rows = document.querySelectorAll("#lights_devices_tbody tr");
    const out = [];
    rows.forEach(tr => {
        const alias = tr.querySelector(".lights-device-alias")?.value?.trim() || "";
        const host = tr.querySelector(".lights-device-host")?.value?.trim() || "";
        const notes = tr.querySelector(".lights-device-notes")?.value?.trim() || "";
        if (alias && host) {
            const d = { alias, host };
            if (notes) d.notes = notes;
            out.push(d);
        }
    });
    return out;
}

function escapeAttrL(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
