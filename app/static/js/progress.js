let socket = null;

const state = {
  selectedAgent: "__all__",
  knownAgents: new Set(),
};

function updateStatus(status, text) {
  const dot = document.querySelector("#status-indicator .status-dot");
  const label = document.querySelector("#status-indicator .status-text");
  if (!dot || !label) return;
  dot.className = "status-dot";
  if (status) dot.classList.add(status);
  label.textContent = text || "";
}

function escapeHtml(str) {
  return String(str || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function ensureAgentOption(agent) {
  if (!agent) return;
  const name = String(agent);
  if (state.knownAgents.has(name)) return;
  state.knownAgents.add(name);

  const select = document.getElementById("progress-agent-filter");
  if (!select) return;
  const opt = document.createElement("option");
  opt.value = name;
  opt.textContent = name;
  select.appendChild(opt);
}

function renderCard(card) {
  const list = document.getElementById("progress-list");
  if (!list) return;

  const agent = card && card.agent ? String(card.agent) : "";
  ensureAgentOption(agent);
  if (state.selectedAgent !== "__all__" && agent !== state.selectedAgent) return;

  const ts = card && card.ts ? new Date(card.ts).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
  const headline = card && card.headline ? String(card.headline) : (card && card.kind ? String(card.kind) : "");
  const goal = card && card.goal ? String(card.goal) : "";
  const learned = card && Array.isArray(card.learned) ? card.learned : [];
  const nextAction = card && card.next && card.next.action ? String(card.next.action) : "";
  const nextInput = card && card.next && card.next.input_preview ? String(card.next.input_preview) : "";
  const nextLine = nextAction ? `${nextAction}${nextInput ? "\n" + nextInput : ""}` : "";
  const toolResultId = card && card.meta && card.meta.tool_result_id ? String(card.meta.tool_result_id) : "";

  const learnedHtml = learned.slice(0, 5).map(x => `<div class="learned">${escapeHtml(x)}</div>`).join("");

  const li = document.createElement("li");
  li.className = "card";
  li.innerHTML = `
    <div class="meta">
      <span>${escapeHtml(ts)}</span>
      <span>${escapeHtml(agent)}</span>
    </div>
    ${headline ? `<div class="headline">${escapeHtml(headline)}</div>` : ""}
    ${goal ? `<div class="goal">${escapeHtml(goal)}</div>` : ""}
    ${learned.length ? `<div class="label">Learned</div>` : ""}
    ${learnedHtml}
    ${nextLine ? `<div class="label">Next</div><div class="next">${escapeHtml(nextLine)}</div>` : ""}
    ${toolResultId ? `<div class="artifact">tool_result_id: <code>${escapeHtml(toolResultId)}</code></div>` : ""}
  `;

  list.prepend(li);
  while (list.children.length > 200) list.removeChild(list.lastChild);
}

function initControls() {
  const select = document.getElementById("progress-agent-filter");
  if (select) {
    const saved = window.localStorage.getItem("progress_selected_agent");
    if (saved) {
      state.selectedAgent = saved;
      select.value = saved;
    }
    select.addEventListener("change", () => {
      state.selectedAgent = select.value || "__all__";
      window.localStorage.setItem("progress_selected_agent", state.selectedAgent);
      const list = document.getElementById("progress-list");
      if (list) list.innerHTML = "";
    });
  }

  const btnClear = document.getElementById("btn-clear");
  if (btnClear) {
    btnClear.addEventListener("click", () => {
      const list = document.getElementById("progress-list");
      if (list) list.innerHTML = "";
    });
  }
}

function initSocket() {
  socket = io();

  if (window.EmiSocketHeartbeat) {
    window.EmiSocketHeartbeat.install({ socket, getRoomId: () => "progress" });
  }

  socket.on("connect", () => {
    updateStatus("ok", "Connected");
    // Register this tab as the PROGRESS client (separate from chat/music).
    socket.emit("register_progress_client", { room_id: "progress" });
  });

  socket.on("connect_error", (err) => {
    console.warn("Socket connect_error:", err);
    updateStatus("error", "Socket failed");
  });

  socket.on("disconnect", (reason) => {
    console.warn("Socket disconnected:", reason);
    updateStatus("error", "Disconnected");
  });

  socket.on("agent_progress_update", (payload) => {
    try {
      renderCard(payload);
    } catch (e) {
      console.error("Failed to render progress payload", e);
    }
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initControls();
  initSocket();
});

