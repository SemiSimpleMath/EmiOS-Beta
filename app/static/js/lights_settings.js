document.addEventListener("DOMContentLoaded", function () {
    bindLightsUiActions();
    loadLightsIntegrationUi();
    bindLightsToolDescriptionActions();
    loadLightsToolDescription();
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
