/* Mobile-only chat client logic (keeps desktop JS untouched) */

// ---- Globals / state ----
let socket;
let speakingMode = false; // toggled via title
let siteMute = false;     // true site mute

let mediaStream = null;
let mediaRecorder = null;
let audioChunks = [];
const MIN_AUDIO_UPLOAD_BYTES = 2048;
const ALLOWED_IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
let pendingImageFile = null;
let pendingImageObjectUrl = null;

// PTT state: tap-to-toggle + hold-to-talk
let pttLocked = false;
let pttPointerDownAt = 0;

// Audio playback
const audioQueue = [];
let isAudioPlaying = false;

// ---- DOM helpers ----
function el(id) { return document.getElementById(id); }

function scrollToBottom() {
  const chatBox = el("chat-box");
  if (chatBox) chatBox.scrollTop = chatBox.scrollHeight;
}

function setHidden(node, hidden) {
  if (!node) return;
  node.classList.toggle("hidden", !!hidden);
}

function syncMuteUi() {
  const muteIcon = el("mute-icon");
  const unmuteIcon = el("unmute-icon");
  if (!muteIcon || !unmuteIcon) return;
  setHidden(muteIcon, siteMute);
  setHidden(unmuteIcon, !siteMute);
}

function syncTtsUi() {
  const t = el("tts-toggle");
  if (!t) return;
  t.setAttribute("aria-pressed", speakingMode ? "true" : "false");
  t.title = speakingMode ? "Text-to-speech: on" : "Text-to-speech: off";
}

function stopAllAudioPlayback() {
  audioQueue.length = 0;
  isAudioPlaying = false;

  const player = el("chat-bot-audio");
  if (player) {
    try { player.pause(); } catch (_) {}
    player.src = "";
  }
  const notif = el("notificationSound");
  if (notif) {
    try { notif.pause(); } catch (_) {}
    notif.src = "";
  }
}

// ---- Chat bubbles ----
function createUserBubble(message) {
  const chatBox = el("chat-box");
  if (!chatBox) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-box-user-bubble";
  bubble.textContent = message;
  chatBox.appendChild(bubble);
  scrollToBottom();
}

function createBotBubble(message) {
  const chatBox = el("chat-box");
  if (!chatBox) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-box-bot-bubble";
  bubble.innerHTML = message; // server may send rich HTML
  chatBox.appendChild(bubble);
  scrollToBottom();
}

// ---- Network send ----
function renderPendingImagePreview() {
  const container = el("pending-image-preview");
  const img = el("pending-image-thumb");
  const nameEl = el("pending-image-name");
  if (!container || !img || !nameEl) return;

  if (!pendingImageFile || !pendingImageObjectUrl) {
    img.src = "";
    nameEl.textContent = "";
    setHidden(container, true);
    return;
  }

  img.src = pendingImageObjectUrl;
  nameEl.textContent = pendingImageFile.name || "image";
  setHidden(container, false);
}

function clearPendingImage() {
  if (pendingImageObjectUrl) {
    try { URL.revokeObjectURL(pendingImageObjectUrl); } catch (_) {}
  }
  pendingImageObjectUrl = null;
  pendingImageFile = null;
  const input = el("image-attach-input");
  if (input) input.value = "";
  renderPendingImagePreview();
}

function setPendingImageFile(file) {
  if (!file) return false;
  if (!ALLOWED_IMAGE_MIMES.has(file.type)) {
    alert("Only PNG, JPEG, WEBP, or GIF images are supported.");
    return false;
  }
  if (pendingImageObjectUrl) {
    try { URL.revokeObjectURL(pendingImageObjectUrl); } catch (_) {}
  }
  pendingImageFile = file;
  pendingImageObjectUrl = URL.createObjectURL(file);
  renderPendingImagePreview();
  return true;
}

function createUserBubbleWithImage(message, imagePreviewUrl = null) {
  const chatBox = el("chat-box");
  if (!chatBox) return;
  const bubble = document.createElement("div");
  bubble.className = "chat-box-user-bubble";
  if (message) {
    const text = document.createElement("div");
    text.textContent = message;
    bubble.appendChild(text);
  }
  if (imagePreviewUrl) {
    const img = document.createElement("img");
    img.className = "chat-image-thumb";
    img.src = imagePreviewUrl;
    img.alt = "Attached image";
    img.onload = () => {
      try { URL.revokeObjectURL(imagePreviewUrl); } catch (_) {}
    };
    bubble.appendChild(img);
  }
  chatBox.appendChild(bubble);
  scrollToBottom();
}

function sendToServer({ text, audioBlob, audioFilename, imageFile }) {
  const formData = new FormData();
  formData.append("socket_id", socket?.id || "");
  formData.append("text", text || "");
  formData.append("speaking_mode", speakingMode ? "true" : "false");

  if (audioBlob) {
    // Keep filename aligned with container to help ffmpeg probing.
    formData.append("audio", audioBlob, audioFilename || "recording.webm");
  }
  if (imageFile && !audioBlob) {
    formData.append("image", imageFile, imageFile.name || "image");
  }

  const route = audioBlob ? "/process_audio" : "/process_request";

  return fetch(route, { method: "POST", body: formData })
    .then(async (response) => {
      let data = {};
      try { data = await response.json(); } catch (_) { /* best-effort */ }
      if (!response.ok) {
        const errMsg = (data && (data.error || data.message)) ? (data.error || data.message) : `HTTP ${response.status}`;
        throw new Error(errMsg);
      }
      return data;
    });
}

// ---- Recording ----
function isCurrentlyRecording() {
  return !!(mediaRecorder && mediaRecorder.state === "recording");
}

function releaseMicStream() {
  if (!mediaStream) return;
  try {
    mediaStream.getTracks().forEach((track) => {
      try { track.stop(); } catch (_) {}
    });
  } catch (_) {}
  mediaStream = null;
}

function isLiveMicStream(stream) {
  if (!stream || stream.active !== true) return false;
  const tracks = stream.getAudioTracks ? stream.getAudioTracks() : [];
  return tracks.some((t) => t && t.readyState === "live");
}

function ensureMicStream() {
  if (isLiveMicStream(mediaStream)) return Promise.resolve(mediaStream);
  releaseMicStream();
  return navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
    mediaStream = stream;
    return stream;
  });
}

function startRecording(stream) {
  audioChunks = [];

  const isIOS = /iP(hone|ad|od)/.test(navigator.userAgent);
  const mp4Codec = "audio/mp4;codecs=mp4a.40.2";
  const webmOpus = "audio/webm;codecs=opus";

  let mimeType = "";
  if (isIOS && MediaRecorder.isTypeSupported(mp4Codec)) {
    mimeType = mp4Codec;
  } else if (MediaRecorder.isTypeSupported("audio/mp4")) {
    mimeType = "audio/mp4";
  } else if (MediaRecorder.isTypeSupported(webmOpus)) {
    mimeType = webmOpus;
  }

  const opts = mimeType ? { mimeType } : {};
  try {
    mediaRecorder = new MediaRecorder(stream, opts);
  } catch (err) {
    console.warn("MediaRecorder fallback init", err);
    mediaRecorder = new MediaRecorder(stream);
  }

  mediaRecorder.ondataavailable = (e) => {
    if (e.data && e.data.size > 0) audioChunks.push(e.data);
  };

  mediaRecorder.onstop = () => {
    const recorderMime = (mediaRecorder && mediaRecorder.mimeType) ? mediaRecorder.mimeType : "";
    const blobType = recorderMime || "audio/webm";
    const blob = new Blob(audioChunks, { type: blobType });
    audioChunks = [];
    mediaRecorder = null;
    releaseMicStream();

    if (blob.size < MIN_AUDIO_UPLOAD_BYTES) {
      alert("Recording was too short or invalid. Please hold to talk a bit longer and try again.");
      return;
    }

    let filename = "recording.webm";
    if (blobType.includes("mp4")) filename = "recording.mp4";
    if (blobType.includes("ogg")) filename = "recording.ogg";

    const input = el("chat-input");
    const msg = (input?.value || "").trim();
    if (msg) {
      createUserBubble(msg);
      if (input) input.value = "";
    }

    // Step 1: upload audio for transcription
    // Step 2: send transcribed text to Emi via /process_request
    sendToServer({ text: msg, audioBlob: blob, audioFilename: filename })
      .then((data) => {
        const t = (data && data.transcribed_text) ? String(data.transcribed_text).trim() : "";
        if (!t) return;

        // Mirror desktop behavior: show transcript as the "real" user message and send it.
        createUserBubble(t);

        if (!socket?.id) {
          throw new Error("Socket not connected yet (missing socket_id). Try again in a second.");
        }
        return sendToServer({ text: t, audioBlob: null });
      })
      .catch((error) => {
        console.error("Error sending audio:", error);
        alert(`An error occurred while sending your audio. ${error?.message ? error.message : ""}`.trim());
      });
  };

  if (isIOS) {
    mediaRecorder.start();
  } else {
    mediaRecorder.start(250);
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === "recording") {
    mediaRecorder.stop();
    return;
  }
  mediaRecorder = null;
  releaseMicStream();
}

// ---- PTT UI ----
function setPttUiState(state) {
  const btn = el("ptt-btn");
  const label = el("ptt-label");
  if (!btn || !label) return;

  btn.dataset.state = state;
  const pressed = state === "recording" ? "true" : "false";
  btn.setAttribute("aria-pressed", pressed);

  if (state === "recording") {
    label.textContent = pttLocked ? "Recording (tap to stop)" : "Recording…";
  } else {
    label.textContent = "Hold to talk";
  }
}

function startPttRecording() {
  if (isCurrentlyRecording()) return;
  return ensureMicStream()
    .then((stream) => {
      startRecording(stream);
      setPttUiState("recording");
    })
    .catch((error) => {
      console.error("Error accessing microphone:", error);
      const errName = error && error.name ? String(error.name) : "";
      if (errName === "NotAllowedError") {
        alert("Microphone permission is blocked. Please allow mic access and try again.");
      } else if (errName === "NotReadableError") {
        alert("Microphone is busy or unavailable right now. Close other apps using the mic and try again.");
      } else {
        alert("Microphone access is required to record audio.");
      }
      setPttUiState("idle");
    });
}

function stopPttRecording() {
  if (!isCurrentlyRecording()) return;
  stopRecording();
  setPttUiState("idle");
}

function bindPtt() {
  const btn = el("ptt-btn");
  if (!btn) return;

  const TAP_THRESHOLD_MS = 230;

  // Prevent "ghost click" behaviors on iOS
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
  btn.addEventListener("selectstart", (e) => e.preventDefault());

  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    btn.setPointerCapture?.(e.pointerId);
    pttPointerDownAt = Date.now();

    // If we're locked and recording, a tap should stop
    if (pttLocked && isCurrentlyRecording()) {
      return;
    }
    startPttRecording();
  }, { passive: false });

  btn.addEventListener("pointerup", (e) => {
    e.preventDefault();

    const elapsed = Date.now() - pttPointerDownAt;

    // Locked mode: any tap stops.
    if (pttLocked && isCurrentlyRecording()) {
      pttLocked = false;
      stopPttRecording();
      return;
    }

    // If user released quickly, treat as toggle-on (keep recording)
    if (isCurrentlyRecording() && elapsed < TAP_THRESHOLD_MS) {
      pttLocked = true;
      setPttUiState("recording");
      return;
    }

    // Otherwise: normal hold-to-talk -> stop on release
    if (!pttLocked) stopPttRecording();
  }, { passive: false });

  btn.addEventListener("pointercancel", (e) => {
    e.preventDefault();
    pttLocked = false;
    stopPttRecording();
  }, { passive: false });
}

// ---- Audio playback from server ----
function playNextAudio() {
  if (siteMute || !speakingMode) return;
  if (isAudioPlaying) return;
  const url = audioQueue.shift();
  if (!url) return;

  const player = el("chat-bot-audio");
  if (!player) return;

  isAudioPlaying = true;
  player.src = url;
  player.onended = () => {
    isAudioPlaying = false;
    playNextAudio();
  };
  player.onerror = () => {
    isAudioPlaying = false;
    playNextAudio();
  };

  player.play().catch((err) => {
    console.warn("Audio play failed:", err);
    isAudioPlaying = false;
  });
}

function setupSocket() {
  socket = io();

  socket.on("connect", () => {
    socket.emit("register_chat_client", { room_id: "master_room" });
  });

  socket.on("user_message_data", (data) => {
    try {
      if (data?.chat && String(data.chat).trim() !== "") {
        createBotBubble(String(data.chat));
      }
      if (Array.isArray(data?.widget_data) && data.widget_data.length > 0) {
        applyAudioControlWidgets(data.widget_data);
      }
    } catch (err) {
      console.error("Error rendering chat:", err);
    }
  });

  socket.on("audio_file", (audioData) => {
    const url = audioData?.audio_url;
    if (!url || typeof url !== "string") return;
    audioQueue.push(url);
    playNextAudio();
  });
}

function applyAudioControlWidgets(widgetDataList) {
  for (const widgetData of widgetDataList) {
    const type = typeof widgetData?.data_type === "string" ? widgetData.data_type.trim().toLowerCase() : "";
    if (type !== "audio_control") continue;
    const actionRaw = widgetData?.action ?? widgetData?.data?.action;
    const action = typeof actionRaw === "string" ? actionRaw.trim().toLowerCase() : "";
    if (action === "mute") {
      siteMute = true;
      syncMuteUi();
      stopAllAudioPlayback();
      continue;
    }
    if (action === "unmute") {
      siteMute = false;
      syncMuteUi();
      continue;
    }
    if (action === "toggle") {
      siteMute = !siteMute;
      syncMuteUi();
      if (siteMute) stopAllAudioPlayback();
      continue;
    }
    console.warn("Unknown audio_control action:", actionRaw);
  }
}

function setupComposer() {
  const form = document.querySelector(".chat-form");
  const input = el("chat-input");
  const attachBtn = el("image-attach-btn");
  const attachInput = el("image-attach-input");
  const removeBtn = el("pending-image-remove");
  if (!form || !input) return;

  // Autosize textarea
  const autosize = () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 130) + "px";
  };
  input.addEventListener("input", autosize);
  autosize();

  input.addEventListener("paste", (event) => {
    const items = event.clipboardData?.items;
    if (!items || !items.length) return;
    for (const item of items) {
      if ((item.type || "").startsWith("image/")) {
        const file = item.getAsFile ? item.getAsFile() : null;
        if (file) {
          event.preventDefault();
          setPendingImageFile(file);
          break;
        }
      }
    }
  });

  if (attachBtn && attachInput) {
    const openPicker = (event) => {
      // iOS Safari can be picky about synthetic clicks; keep this as fallback.
      if (event) event.preventDefault();
      try { attachInput.click(); } catch (_) {}
    };
    attachBtn.addEventListener("click", openPicker);
    attachBtn.addEventListener("touchend", openPicker, { passive: false });
    attachBtn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") openPicker(e);
    });
    attachInput.addEventListener("change", () => {
      const file = attachInput.files && attachInput.files[0] ? attachInput.files[0] : null;
      if (file) setPendingImageFile(file);
    });
  }
  if (removeBtn) {
    removeBtn.addEventListener("click", () => clearPendingImage());
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const msg = (input.value || "").trim();
    const hasImage = !!pendingImageFile;
    if (!msg && !hasImage) return;
    const bubbleImageUrl = hasImage ? URL.createObjectURL(pendingImageFile) : null;
    createUserBubbleWithImage(msg, bubbleImageUrl);
    input.value = "";
    autosize();
    sendToServer({ text: msg, audioBlob: null, imageFile: hasImage ? pendingImageFile : null }).then(() => {
      if (hasImage) clearPendingImage();
    }).catch((error) => {
      console.error("Error sending message:", error);
      alert(`An error occurred while sending your message. ${error?.message ? error.message : ""}`.trim());
    });
  });
}

function setupHeaderToggles() {
  const ttsToggle = el("tts-toggle");
  if (ttsToggle) {
    const toggle = () => {
      speakingMode = !speakingMode;
      syncTtsUi();
      if (!speakingMode) stopAllAudioPlayback();
    };
    ttsToggle.addEventListener("click", toggle);
    ttsToggle.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  }

  const muteBtn = el("mute-btn-container");
  if (muteBtn) {
    const toggle = () => {
      siteMute = !siteMute;
      syncMuteUi();
      if (siteMute) stopAllAudioPlayback();
    };
    muteBtn.addEventListener("click", toggle);
    muteBtn.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  }
}

// ---- Boot ----
document.addEventListener("DOMContentLoaded", () => {
  syncTtsUi();
  syncMuteUi();
  setPttUiState("idle");
  renderPendingImagePreview();

  setupSocket();
  setupComposer();
  setupHeaderToggles();
  bindPtt();

  // iOS can keep audio in "communication" routing if mic tracks stay live.
  window.addEventListener("pagehide", () => {
    if (isCurrentlyRecording()) {
      stopRecording();
      return;
    }
    mediaRecorder = null;
    releaseMicStream();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) return;
    if (isCurrentlyRecording()) {
      stopRecording();
      return;
    }
    mediaRecorder = null;
    releaseMicStream();
  });
});

