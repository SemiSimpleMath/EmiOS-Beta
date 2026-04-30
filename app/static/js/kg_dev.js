(function () {
  "use strict";

  const ROOM_ID = "kg_dev_room";

  const logEl = document.getElementById("kg-dev-log");
  const inputEl = document.getElementById("kg-dev-input");
  const formEl = document.getElementById("kg-dev-composer");
  const sendBtn = document.getElementById("kg-dev-send");
  const statusEl = document.getElementById("kg-dev-status");

  let socket = null;

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "kg-dev-status" + (cls ? " " + cls : "");
  }

  function addMsg(text, role) {
    const div = document.createElement("div");
    div.className = "kg-dev-msg " + (role || "system");
    div.textContent = String(text || "");
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function initSocket() {
    socket = io({ transports: ["websocket", "polling"] });

    if (window.EmiSocketHeartbeat) {
      window.EmiSocketHeartbeat.install({ socket: socket, getRoomId: () => ROOM_ID });
    }

    socket.on("connect", function () {
      const payload = { room_id: ROOM_ID };
      const since = window.EmiSocketHeartbeat && window.EmiSocketHeartbeat.getLastMessageTs();
      if (since) payload.since_ts = since;
      socket.emit("register_chat_client", payload);
      setStatus("ready", "ready");
      sendBtn.disabled = false;
    });

    socket.on("disconnect", function () {
      setStatus("disconnected", "error");
      sendBtn.disabled = true;
    });

    socket.on("user_message_data", function (data) {
      if (!data) return;
      if (window.EmiSocketHeartbeat) window.EmiSocketHeartbeat.noteIncoming(data);

      // Skip messages not meant for this room.
      const targetRoom = data.room_id || (data.metadata && data.metadata.room_id);
      if (targetRoom && targetRoom !== ROOM_ID) return;

      // Skip proactive / situation-audit broadcasts.
      const subTypes = data.sub_data_type || [];
      if (Array.isArray(subTypes) && (subTypes.indexOf("proactive") >= 0 || subTypes.indexOf("situation_audit") >= 0)) {
        return;
      }

      if (data.chat && typeof data.chat === "string" && data.chat.trim()) {
        addMsg(data.chat, "assistant");
        setStatus("ready", "ready");
        sendBtn.disabled = false;
      }
    });
  }

  function send(text) {
    if (!text || !text.trim()) return;
    addMsg(text, "user");
    setStatus("running query…", "");
    sendBtn.disabled = true;

    const formData = new FormData();
    formData.append("text", text);
    formData.append("room_id", ROOM_ID);

    fetch("/process_request", { method: "POST", body: formData })
      .catch(function (err) {
        addMsg("send failed: " + err.message, "system");
        setStatus("error", "error");
        sendBtn.disabled = false;
      });
  }

  formEl.addEventListener("submit", function (e) {
    e.preventDefault();
    const text = inputEl.value;
    inputEl.value = "";
    send(text);
  });

  inputEl.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      formEl.requestSubmit();
    }
  });

  setStatus("connecting…");
  sendBtn.disabled = true;
  initSocket();

  addMsg("KG Dev Console ready. Ask any KG-related question — the planner runs read-only SQL via kg_query and pod tools to answer.", "system");
})();
