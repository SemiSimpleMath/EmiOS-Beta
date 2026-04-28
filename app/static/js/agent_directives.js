(function () {
    "use strict";

    var agents = [];
    var currentIndex = 0;
    var dirty = false;

    var editorEl = document.getElementById("directive-editor");
    var nameEl = document.getElementById("agent-name");
    var descEl = document.getElementById("agent-description");
    var statusEl = document.getElementById("status-msg");
    var saveBtn = document.getElementById("save-btn");
    var prevBtn = document.getElementById("prev-agent");
    var nextBtn = document.getElementById("next-agent");

    function showStatus(msg, isError) {
        statusEl.textContent = msg;
        statusEl.style.color = isError ? "#ff6b6b" : "#6bff6b";
        setTimeout(function () { statusEl.textContent = ""; }, 3000);
    }

    function loadAgentList() {
        fetch("/api/agent-directives")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                agents = data.agents || [];
                if (agents.length > 0) {
                    loadAgent(0);
                }
            })
            .catch(function (e) {
                showStatus("Failed to load agents: " + e, true);
            });
    }

    function loadAgent(index) {
        if (index < 0 || index >= agents.length) return;

        if (dirty) {
            if (!confirm("You have unsaved changes. Discard?")) return;
        }

        currentIndex = index;
        var agent = agents[index];
        nameEl.textContent = agent.display_name;
        descEl.textContent = agent.description;

        fetch("/api/agent-directives/" + agent.key)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                editorEl.value = data.content || "";
                dirty = false;
            })
            .catch(function (e) {
                showStatus("Failed to load directive: " + e, true);
            });
    }

    function saveCurrentAgent() {
        var agent = agents[currentIndex];
        if (!agent) return;

        fetch("/api/agent-directives/" + agent.key, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: editorEl.value })
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.status === "saved") {
                    showStatus("Saved.", false);
                    dirty = false;
                } else {
                    showStatus("Error: " + (data.error || "unknown"), true);
                }
            })
            .catch(function (e) {
                showStatus("Save failed: " + e, true);
            });
    }

    editorEl.addEventListener("input", function () { dirty = true; });

    saveBtn.addEventListener("click", saveCurrentAgent);

    prevBtn.addEventListener("click", function () {
        if (agents.length === 0) return;
        var idx = (currentIndex - 1 + agents.length) % agents.length;
        loadAgent(idx);
    });

    nextBtn.addEventListener("click", function () {
        if (agents.length === 0) return;
        var idx = (currentIndex + 1) % agents.length;
        loadAgent(idx);
    });

    document.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "s") {
            e.preventDefault();
            saveCurrentAgent();
        }
    });

    loadAgentList();
})();
