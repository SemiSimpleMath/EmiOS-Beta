document.addEventListener("DOMContentLoaded", () => {
    const saveBtn = document.getElementById("saveImportantResourcesBtn");
    if (saveBtn) {
        saveBtn.addEventListener("click", saveImportantResources);
    }
    loadImportantResources();
});

function showError(message) {
    const el = document.getElementById("error-message");
    if (!el) return;
    el.textContent = message;
    el.style.display = "block";
    setTimeout(() => {
        el.style.display = "none";
    }, 6000);
}

function showSuccess(message) {
    const el = document.getElementById("success-message");
    if (!el) return;
    el.textContent = message;
    el.style.display = "block";
    setTimeout(() => {
        el.style.display = "none";
    }, 3000);
}

function escapeHtml(value) {
    return String(value || "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll("\"", "&quot;")
        .replaceAll("'", "&#39;");
}

async function loadImportantResources() {
    try {
        const res = await fetch("/api/settings/important-resources");
        const data = await res.json();
        if (!res.ok || !data.success) {
            throw new Error(data.error || "Failed to load important resources.");
        }
        renderResourceEditors(data.resources || []);
    } catch (error) {
        console.error("loadImportantResources failed:", error);
        showError(`Failed to load important resources: ${error.message}`);
    }
}

function renderResourceEditors(resources) {
    const container = document.getElementById("resourceEditors");
    if (!container) return;
    container.innerHTML = "";

    for (const resource of resources) {
        const id = String(resource.id || "").trim();
        const title = String(resource.title || id || "Resource");
        const description = String(resource.description || "");
        const path = String(resource.path || "");
        const content = String(resource.content || "");
        const kind = String(resource.kind || "");
        if (!id) continue;

        const card = document.createElement("div");
        card.className = "resource-editor-card";
        card.innerHTML = `
            <h3>${escapeHtml(title)}</h3>
            <p class="resource-editor-description">${escapeHtml(description)}</p>
            <div class="resource-editor-path">${escapeHtml(kind === "json_blob" ? "Editor format: JSON object" : "Editor format: text content")}</div>
            <div class="resource-editor-path">${escapeHtml(path)}</div>
            <textarea
                id="resource-editor-${escapeHtml(id)}"
                class="resource-editor-textarea"
                data-resource-id="${escapeHtml(id)}"
                spellcheck="false"
            >${escapeHtml(content)}</textarea>
        `;
        container.appendChild(card);
    }
}

function collectResourcesPayload() {
    const editors = document.querySelectorAll(".resource-editor-textarea[data-resource-id]");
    const resources = [];
    editors.forEach((editor) => {
        const id = String(editor.getAttribute("data-resource-id") || "").trim();
        if (!id) return;
        resources.push({
            id,
            content: editor.value,
        });
    });
    return { resources };
}

async function saveImportantResources() {
    const saveBtn = document.getElementById("saveImportantResourcesBtn");
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Saving...';
    }
    try {
        const payload = collectResourcesPayload();
        if (!Array.isArray(payload.resources) || payload.resources.length === 0) {
            throw new Error("No editable resources found on this page.");
        }

        const res = await fetch("/api/settings/important-resources", {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok || !data.success) {
            throw new Error(data.error || "Failed to save important resources.");
        }
        showSuccess("Important resources saved successfully.");
    } catch (error) {
        console.error("saveImportantResources failed:", error);
        showError(`Failed to save important resources: ${error.message}`);
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.innerHTML = '<i class="fas fa-save"></i> Save Changes';
        }
    }
}
