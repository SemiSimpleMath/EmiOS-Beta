// ==================== Global Variables ====================
let audioChunks = [];
let isRecording = false;
let mediaRecorder;
let mediaStream;
let socket = io.connect(`${window.location.protocol}//${document.domain}:${location.port}`);
// Global accessor so other modules (e.g. EmiTaskCreatePanel) can get the live socket id.
window.EmiGetSocketId = () => (socket ? socket.id : "");
let audioQueue = [];
let isAudioPlaying = false;
let mute = false;
let lastReceivedResponse = "";
let lastInteractionTime = 0;
let ttsAudioQueue = [];
let isTTSPlaying = false;
let speakingMode = false;

// Chat mode state management
let chatMode = 'normal'; // 'normal', 'test', 'memo'

function getEmiChatModeContext() {
    return {
        modeIndicatorId: "mode-indicator",
        notificationId: "mode-notification",
        notificationClassName: "mode-notification",
        getChatMode: () => chatMode,
        setChatMode: (mode) => { chatMode = mode; },
    };
}

// ===== Image attachment state (paste/drag/drop) =====
const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
const ALLOWED_IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
let pendingImageFile = null;
let pendingImageObjectUrl = null;

function getEmiAttachmentsContext() {
    return {
        previewContainerId: "pending-image-preview",
        previewThumbId: "pending-image-thumb",
        previewNameId: "pending-image-name",
        getPendingImageFile: () => pendingImageFile,
        setPendingImageFile: (file) => { pendingImageFile = file || null; },
        getPendingImageObjectUrl: () => pendingImageObjectUrl,
        setPendingImageObjectUrl: (url) => { pendingImageObjectUrl = url || null; },
        getAllowedImageMimes: () => ALLOWED_IMAGE_MIMES,
        getMaxImageBytes: () => MAX_IMAGE_BYTES,
        alertUser: (message) => alert(message),
        logWarn: (...args) => console.warn(...args),
    };
}

function getPendingImageFile() {
    return window.EmiAttachments.getPendingImageFile(getEmiAttachmentsContext());
}

function updatePendingImagePreviewUI() {
    window.EmiAttachments.updatePendingImagePreviewUI(getEmiAttachmentsContext());
}

function clearPendingImage() {
    window.EmiAttachments.clearPendingImage(getEmiAttachmentsContext());
}

function setPendingImage(file) {
    window.EmiAttachments.setPendingImage(file, getEmiAttachmentsContext());
}

// Mode management functions
function toggleTestMode() {
    return window.EmiChatMode.toggleTestMode(getEmiChatModeContext());
}

function toggleMemoMode() {
    return window.EmiChatMode.toggleMemoMode(getEmiChatModeContext());
}

function getEmiRequestSenderContext() {
    return {
        getChatMode: () => chatMode,
        setChatMode: (mode) => { chatMode = mode; },
        updateModeIndicator,
        showModeNotification,
        getSocketId: () => (socket ? socket.id : ""),
        getPendingImageFile,
        clearPendingImage,
        getLastInteractionTime: () => lastInteractionTime,
        setLastInteractionTime: (v) => { lastInteractionTime = Number(v) || 0; },
        getSpeakingMode: () => speakingMode,
        createUserBubble,
        sendAgain: (text, blob) => prepareDataToBeSent(text, blob),
        clearAudioChunks: () => { audioChunks = []; },
        resetRecordingUI: () => {
            isRecording = false;
            const recordBtnOff = document.getElementById("record-btn-off");
            const recordBtnOn = document.getElementById("record-btn-on");
            const recordBtnIcon = document.getElementById("record-btn-icon");
            if (recordBtnOff) recordBtnOff.classList.remove("hidden");
            if (recordBtnOn) recordBtnOn.classList.add("hidden");
            if (recordBtnIcon) recordBtnIcon.classList.remove("active");
        },
        alertUser: (message) => alert(message),
        logDebug: (...args) => console.log(...args),
        logError: (...args) => console.error(...args),
    };
}

// Parse commands and return mode and text
function parseCommand(message) {
    return window.EmiRequestSender.parseCommand(message, getEmiRequestSenderContext());
}

// Update visual mode indicator
function updateModeIndicator() {
    window.EmiChatMode.updateModeIndicator(getEmiChatModeContext());
}

// Show mode change notification
function showModeNotification(message) {
    window.EmiChatMode.showModeNotification(message, getEmiChatModeContext());
}

let idleTimeout = null; // Reference to the timeout function
let idleThreshold = 1 * 60 * 1000; // 1 minute
let idleInterval = null; // Reference to the recurring interval function
let idleCheckInterval = 1 * 60 * 1000; // 1 minute

function getEmiIdleContext() {
    return {
        getIdleTimeout: () => idleTimeout,
        setIdleTimeout: (v) => { idleTimeout = v || null; },
        getIdleInterval: () => idleInterval,
        setIdleInterval: (v) => { idleInterval = v || null; },
        getIdleThresholdMs: () => Number(idleThreshold) || 60000,
        getIdleCheckIntervalMs: () => Number(idleCheckInterval) || 60000,
        getSocketId: () => (socket ? socket.id : ""),
        getPreferenceList: () => window.EmiUiState.getPreferences(),
        clearPreferenceList: () => window.EmiUiState.clearPreferences(),
        clearAudioChunks: () => { audioChunks = []; },
        fetchImpl: (...args) => fetch(...args),
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiMenuControlsContext() {
    const notifyFn =
        (typeof showNotification === "function")
            ? showNotification
            : (message, _level) => showModeNotification(String(message || ""));
    return {
        getSpeakingMode: () => speakingMode,
        setSpeakingMode: (isOn) => setSpeakingMode(!!isOn),
        getAudioPlayer: () => audioPlayer,
        showModeNotification,
        showNotification: notifyFn,
        fetchImpl: (...args) => fetch(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiCalendarWidgetsContext() {
    return {
        getCurrentCalendarDate: () => currentCalendarDate,
        setCurrentCalendarDate: (d) => { currentCalendarDate = d instanceof Date ? d : new Date(d); },
        getCalendarEventsByDate: () => calendarEventsByDate,
        getFormattedDate,
        formatKeyForDisplay,
        formatValue,
        logDebug: (...args) => console.log(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiSchedulerWidgetsContext() {
    return {
        getCurrentSchedulerDate: () => currentSchedulerDate,
        setCurrentSchedulerDate: (d) => { currentSchedulerDate = d instanceof Date ? d : new Date(d); },
        getSchedulerEventsByDate: () => schedulerEventsByDate,
        getFormattedDate,
        formatKeyForDisplay,
        formatValue,
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiTodoWidgetsContext() {
    return {
        callToolRoute,
        addFeedItem,
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiEmailWidgetsContext() {
    return {
        formatKeyForDisplay,
        formatValue,
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiNewsWeatherWidgetsContext() {
    return {
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiWidgetHandlersContext() {
    return {
        getHandlers: () => ({
            email: handleEmailWidget,
            calendar: handleCalendarWidget,
            scheduler: handleSchedulerWidget,
            news: renderNewsWidget,
            weather: updateWeatherWidget,
            todo_task: handleTodoWidget,
            ask_user: showAskUserModal,
            notify_user: showNotifyUserModal,
            audio_control: handleAudioControlWidget,
            redirect: (items) => { const url = items[0] && items[0].url; if (url) window.location.href = url; },
            task_draft_update: (items) => window.EmiTaskCreatePanel && window.EmiTaskCreatePanel.dispatchWidget(items),
            task_compiling: (items) => window.EmiTaskCreatePanel && window.EmiTaskCreatePanel.dispatchWidget(items),
            task_compiled: (items) => window.EmiTaskCreatePanel && window.EmiTaskCreatePanel.dispatchWidget(items),
            task_compile_failed: (items) => window.EmiTaskCreatePanel && window.EmiTaskCreatePanel.dispatchWidget(items),
            task_load_request: (items) => window.EmiTaskCreatePanel && window.EmiTaskCreatePanel.dispatchWidget(items),
            task_running_notice: handleTaskRunningNotice,
            doc_draft_update: (items) => window.EmiDocPanel && window.EmiDocPanel.dispatchWidget(items),
            doc_created: (items) => window.EmiDocPanel && window.EmiDocPanel.dispatchWidget(items),
            doc_save_failed: (items) => window.EmiDocPanel && window.EmiDocPanel.dispatchWidget(items),
            doc_open_empty: (items) => window.EmiDocPanel && window.EmiDocPanel.dispatchWidget(items),
            doc_load_request: (items) => window.EmiDocPanel && window.EmiDocPanel.dispatchWidget(items),
            geo_update: (items) => window.EmiGeoPanel && window.EmiGeoPanel.handleGeoUpdate(items[0] || {}),
            geo_reveal: (items) => window.EmiGeoPanel && window.EmiGeoPanel.handleGeoReveal(items[0] || {}),
        }),
        handleGenericWidget: (items) => handleGenericWidget(items),
        normalizeWidgetGroup: (widgetType, widgetItems) => window.EmiWidgetContract.normalizeWidgetGroup(
            widgetType,
            widgetItems,
            { logWarn: (...args) => console.warn(...args) },
        ),
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function resolveWidgetHandler(widgetType) {
    return window.EmiWidgetHandlers.getWidgetHandler(widgetType, getEmiWidgetHandlersContext());
}

// Array of sound file paths
const randomSoundFiles = [
    "/static/sounds/notifications/bellding-254774.mp3",
    "/static/sounds/notifications/ding-sound-246413.mp3",
    "/static/sounds/notifications/notification-5-158190.mp3",
    "/static/sounds/notifications/notification-beep-229154.mp3",
];

let soundLib = {
    reminder: "/static/sounds/notifications/reminder-tone.mp3",
    drink_water_temp_disabled: "/static/sounds/notifications/drink_water.mp3",
    stretch_fingers: "/static/sounds/notifications/bellding-254774.mp3",
    job_done: "/static/sounds/notifications/jobs-done_1.mp3",
    ding: "/static/sounds/notifications/ding-sound-246413.mp3",
    default_sound: "/static/sounds/notifications/notification-beep-229154.mp3"
};

// at top of emi.js
let audioPlayer;  // declare in outer scope
function updateSpeakModeUi(isOn) {
    const menuSpeakBtn = document.getElementById('menu-speak-mode');
    if (menuSpeakBtn) {
        const span = menuSpeakBtn.querySelector('span');
        if (span) {
            span.textContent = isOn ? 'Speak Mode: On' : 'Speak Mode: Off';
        }
        menuSpeakBtn.setAttribute('aria-pressed', isOn);
    }
}

function syncSiteMuteUi() {
    const muteIcon = document.getElementById('mute-icon');
    const unmuteIcon = document.getElementById('unmute-icon');
    if (muteIcon && unmuteIcon) {
        // Site mute: show volume icon when unmuted, muted icon when muted
        muteIcon.classList.toggle('hidden', mute);
        unmuteIcon.classList.toggle('hidden', !mute);
    }
}

function syncTtsTitleUi() {
    const el = document.getElementById('tts-toggle');
    if (!el) return;
    el.setAttribute('aria-pressed', speakingMode ? 'true' : 'false');
    el.dataset.tts = speakingMode ? 'on' : 'off';
    el.title = speakingMode ? 'Speak mode ON — click to turn off' : 'Click to turn on speak mode';
}

function getEmiAudioContext() {
    return {
        getMute: () => mute,
        setMute: (v) => { mute = !!v; },
        getSpeakingMode: () => speakingMode,
        setSpeakingMode: (v) => { speakingMode = !!v; },
        getAudioQueue: () => audioQueue,
        setAudioQueue: (arr) => { audioQueue = Array.isArray(arr) ? arr : []; },
        getIsAudioPlaying: () => isAudioPlaying,
        setIsAudioPlaying: (v) => { isAudioPlaying = !!v; },
        getTtsAudioQueue: () => ttsAudioQueue,
        setTtsAudioQueue: (arr) => { ttsAudioQueue = Array.isArray(arr) ? arr : []; },
        getIsTtsPlaying: () => isTTSPlaying,
        setIsTtsPlaying: (v) => { isTTSPlaying = !!v; },
        getAudioPlayer: () => audioPlayer,
        syncSiteMuteUi,
        syncTtsTitleUi,
        updateSpeakModeUi,
        playNextAudio,
        playNextTTSAudio,
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function getEmiRecorderContext() {
    return {
        getAudioChunks: () => audioChunks,
        setAudioChunks: (chunks) => { audioChunks = Array.isArray(chunks) ? chunks : []; },
        getIsRecording: () => isRecording,
        setIsRecording: (v) => { isRecording = !!v; },
        getMediaRecorder: () => mediaRecorder,
        setMediaRecorder: (r) => { mediaRecorder = r || null; },
        getMediaStream: () => mediaStream,
        setMediaStream: (s) => { mediaStream = s || null; },
        createUserBubble,
        prepareDataToBeSent,
        getMessageInput: () => document.getElementById("chat-input"),
        alertUser: (message) => alert(message),
        resetRecordingUI: () => {
            // Force mic button back to "off" state so it's usable after an error
            isRecording = false;
            const recordBtnOff = document.getElementById("record-btn-off");
            const recordBtnOn = document.getElementById("record-btn-on");
            const recordBtnIcon = document.getElementById("record-btn-icon");
            if (recordBtnOff) recordBtnOff.classList.remove("hidden");
            if (recordBtnOn) recordBtnOn.classList.add("hidden");
            if (recordBtnIcon) recordBtnIcon.classList.remove("active");
        },
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    };
}

function stopAllAudioPlayback() {
    window.EmiAudio.stopAllAudioPlayback(getEmiAudioContext());
}

function setSpeakingMode(isOn) {
    console.log(`[TTS] setSpeakingMode(${isOn})`);
    window.EmiAudio.setSpeakingMode(isOn, getEmiAudioContext());
}

function setSiteMute(isMuted) {
    window.EmiAudio.setSiteMute(isMuted, getEmiAudioContext());
}

function setupSiteMuteToggle() {
    // The site-mute speaker icon was retired when light/dark + speak +
    // quiet moved into the gear dropdown. Speak Mode is now the user-
    // facing audio control. Leaving this no-op so callers don't have to
    // be guarded; site mute defaults to false.
    const btn = document.getElementById('mute-btn-container');
    if (!btn || btn.dataset.boundMute === "true") return;
    btn.dataset.boundMute = "true";
    btn.addEventListener('click', () => { setSiteMute(!mute); });
}

function setupTtsTitleToggle() {
    const el = document.getElementById('tts-toggle');
    if (!el) return;
    if (el.dataset.boundTts === "true") return;
    el.dataset.boundTts = "true";

    const toggle = () => setSpeakingMode(!speakingMode);
    el.addEventListener('click', toggle);
    el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            toggle();
        }
    });
}

document.addEventListener('DOMContentLoaded', () => {
  const speakBtn = document.getElementById('speak-mode-btn');
  audioPlayer = document.getElementById('chat-bot-audio');

  // Sync TTS UI to current speaking mode.
  updateSpeakModeUi(speakingMode);
  syncTtsTitleUi();
  syncSiteMuteUi();
  setupTtsTitleToggle();
  setupSiteMuteToggle();

  let audioUnlocked = false;
  const SILENT = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=";

  function unlockAudio(reason) {
    if (audioUnlocked || !audioPlayer) return;

    audioPlayer.src = SILENT;
    audioPlayer.play()
      .then(() => {
        audioPlayer.pause();
        audioPlayer.src = "";
        audioUnlocked = true;
        console.log(`🔓 Audio unlocked (${reason})`);
      })
      .catch(e => console.warn(`🔒 Unlock failed (${reason})`, e));
  }

  // Speak mode toggle button
  if (speakBtn) {
    speakBtn.addEventListener('click', () => {
      setSpeakingMode(!speakingMode);

      // Try unlocking on button click (works on iOS)
      if (/iP(hone|ad|od)/.test(navigator.userAgent)) {
        unlockAudio("Speak Mode toggle");
      }
    });
  }

  // Additional iOS unlock hook on first touch
  if (/iP(hone|ad|od)/.test(navigator.userAgent)) {
    const touchUnlock = () => {
      unlockAudio("first touch");
      document.removeEventListener("touchend", touchUnlock, true);
    };
    document.addEventListener("touchend", touchUnlock, true);
  }
});


/**
Boot up routine
*/

async function fetchRepoData() {
    console.log("📦 Fetching repo data from /render_repo_route...");
    try {
        const response = await fetch('/render_repo_route', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({})
        });

        if (!response.ok) {
            console.error(`❌ HTTP error fetching repo data! Status: ${response.status}`);
            throw new Error(`HTTP error! Status: ${response.status}`);
        }

        const data = await response.json();
        console.log("📦 Repo data response:", data);

        if (data.success && data.repo_data) {
            const itemCount = data.repo_data.widget_data ? data.repo_data.widget_data.length : 0;
            console.log(`✅ Processing ${itemCount} repo items`);
            processRepoData(data.repo_data);
        } else {
            console.warn("⚠️ Repo data response was not successful or missing repo_data:", data);
            if (data.message) {
                console.error("   Server message:", data.message);
            }
        }
    } catch (error) {
        console.error("🛑 Error fetching repo data:", error);
        console.error("   This may indicate the backend is not running or there's a database issue.");
    }
}

/**
 * Loads recent chat history on page init so the conversation isn't blank
 * after a page refresh or reconnect.
 */
async function loadChatHistory() {
    try {
        const response = await fetch("/api/chat/history");
        if (!response.ok) return;
        const data = await response.json();
        if (!data.success || !Array.isArray(data.messages)) return;

        const chatBox = document.getElementById("chat-box");
        if (!chatBox) return;

        for (const msg of data.messages) {
            if (msg.role === "user") {
                createUserBubble(msg.content);
            } else if (msg.role === "assistant") {
                createBotBubble(msg.content);
            }
        }
        scrollToBottom();
        console.log(`Restored ${data.messages.length} chat message(s) from history.`);
    } catch (e) {
        console.error("Failed to load chat history:", e);
    }
}

async function sendAskUserResponse(questionData, answer) {
    console.log("At ask_user_answer route");
    const payload = {
        question_id: questionData?.question_id,
        answer: answer,
        request_id: questionData?.request_id || null,
        room_id: questionData?.room_id || null,
        reply_to: (questionData && typeof questionData.reply_to === "object") ? questionData.reply_to : null,
        user_identity: questionData?.user_identity || null,
    };
    try {
        const response = await fetch('/ask_user_answer', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            throw new Error(`HTTP error! Status: ${response.status}`);
        }

        const data = await response.json();
        console.log("Response from ask_user_answer:", data);
        // Optionally process data from the route if needed

    } catch (error) {
        console.error("Error sending ask user response:", error);
    }
}


function processRepoData(repoData) {
    console.log("Processing Repo Data");
    window.EmiWidgetHandlers.processRepoWidgetData(repoData, getEmiWidgetHandlersContext());
}



/**
 * Plays a random notification sound from the randomSoundFiles array.
 */
function playRandomNotificationSound() {
    if (mute) return;

    // **Correction Applied: Changed 'soundFiles' to 'randomSoundFiles'**
    if (!randomSoundFiles || randomSoundFiles.length === 0) { // Corrected variable
        console.warn("No sound files available to play.");
        return;
    }

    const randomIndex = Math.floor(Math.random() * randomSoundFiles.length);
    const selectedSound = randomSoundFiles[randomIndex];
    console.log(`Playing random notification sound: ${selectedSound}`);

    playSound(selectedSound);
}

/**
 * Adds or updates a preference in the preferenceList.
 * @param {string} category - The category of the content (e.g., 'news').
 * @param {string} title - The title of the news item.
 * @param {string} body - The body or summary of the news item.
 * @param {string} preference - The preference type ('like' or 'dislike').
 * @param {string} id - The unique identifier for the news item (e.g., URL or ID).
 */
function addOrUpdatePreference(category, title, body, preference, id) {
    window.EmiNewsWeatherWidgets.addOrUpdatePreference(
        category,
        title,
        body,
        preference,
        id,
        getEmiNewsWeatherWidgetsContext(),
    );
}

/**
 * Removes a preference from the preferenceList.
 * @param {string} id - The unique identifier for the news item (e.g., URL or ID).
 */
function removePreference(id) {
    window.EmiNewsWeatherWidgets.removePreference(id, getEmiNewsWeatherWidgetsContext());
}

// **Correction Applied: Removed initial event listeners referencing undefined functions**
/*
document.querySelectorAll('.like-button').forEach(button => {
    button.addEventListener('click', () => {
        const newsItem = getNewsItem(button); // Undefined function
        addPreference('news', newsItem.title, newsItem.body, 'like'); // Undefined function
    });
});

document.querySelectorAll('.dislike-button').forEach(button => {
    button.addEventListener('click', () => {
        const newsItem = getNewsItem(button); // Undefined function
        addPreference('news', newsItem.title, newsItem.body, 'dislike'); // Undefined function
    });
});
*/

/**
 * Handles thumbs-up or thumbs-down actions.
 * @param {string} action - 'up' or 'down'.
 * @param {Event} event - The event triggered by the button click.
 */
function handleThumbs(action, event) {
    window.EmiNewsWeatherWidgets.handleThumbs(action, event, getEmiNewsWeatherWidgetsContext());
}

// ==================== Calendar Variables ====================
let currentCalendarDate = new Date(); // Tracks the currently displayed date
const calendarEventsByDate = {}; // Stores events grouped by date in 'YYYY-MM-DD' format

// ==================== Utility Functions ====================

/**
 * Formats a Date object to 'YYYY-MM-DD' based on local time.
 * @param {Date} date - The date to format.
 * @returns {string} - Formatted date string.
 */
function getFormattedDate(date) {
    const year = date.getFullYear();
    const month = (`0${date.getMonth() + 1}`).slice(-2); // Months are zero-based
    const day = (`0${date.getDate()}`).slice(-2);
    return `${year}-${month}-${day}`;
}

/**
 * Formats a key string for display by replacing underscores with spaces and capitalizing each word.
 * @param {string} key - The key string to format.
 * @returns {string} - The formatted key string.
 */
function formatKeyForDisplay(key) {
    return key.replace(/_/g, ' ').replace(/\b\w/g, char => char.toUpperCase());
}

/**
 * Formats the value based on the key.
 * @param {string} key - The key corresponding to the value.
 * @param {*} value - The value to format.
 * @returns {string} - The formatted value string.
 */
function formatValue(key, value) {
    // Handle date and time formatting
    const dateKeys = ['start_time', 'end_time', 'start', 'end', 'start_dateTime', 'end_dateTime'];
    if (dateKeys.includes(key) && value) {
        return new Date(value).toLocaleString();
    }

    // Handle link formatting
    if (key.toLowerCase().includes('link') && value) {
        return `<a href="${value}" target="_blank">${value}</a>`;
    }

    // Handle participants or similar arrays
    if (Array.isArray(value)) {
        return value.join(', ');
    }

    // For other types, return as is
    return value;
}

/**
 * Scrolls the chat box to the bottom.
 */
function scrollToBottom() {
    const chatBox = document.getElementById("chat-box");
    if (chatBox) {
        chatBox.scrollTop = chatBox.scrollHeight;
    } else {
        console.error("Chat box element with ID 'chat-box' not found.");
    }
}


/**
 * Plays the next audio in the audioQueue.
 */
function playNextAudio() {
    window.EmiAudio.playNextAudio(getEmiAudioContext());
}

/**
 * Plays the next TTS audio in the ttsAudioQueue.
 */
function playNextTTSAudio() {
    window.EmiAudio.playNextTTSAudio(getEmiAudioContext());
}


/**
 * Resets the idle timer whenever user activity is detected.
 */
function resetIdleTimer() {
    window.EmiIdleManager.resetIdleTimer(getEmiIdleContext());
}


/**
 * Invokes the idle route on the server when the user is idle.
 */
function invokeIdleRoute() {
    window.EmiIdleManager.invokeIdleRoute(getEmiIdleContext());
}

/**
 * Attaches event listeners to detect user activity and reset the idle timer.
 */
function attachIdleListeners() {
    window.EmiIdleManager.attachIdleListeners(getEmiIdleContext());
}

function setupIdleVisibilityHooks() {
    window.EmiIdleManager.setupVisibilityIdleHooks(getEmiIdleContext());
}

// ==================== Chat Bubble Functions ====================

/**
 * Creates a new bot chat bubble with the given text.
 * @param {string} text - The bot's message.
 * @param {object} [meta] - Optional metadata (e.g. sub_data_type).
 */
function createBotBubble(text, meta) {
    var bubbleClass = "bot-bubble";
    if (meta && Array.isArray(meta.sub_data_type) && meta.sub_data_type.indexOf("proactive") !== -1) {
        bubbleClass = "bot-bubble bot-bubble-proactive";
    }
    window.EmiChatRender.createBotBubble(text, {
        chatBoxId: "chat-box",
        bubbleClass: bubbleClass,
        scrollToBottom,
        logError: (...args) => console.error(...args),
    });
}

/**
 * Creates a new user chat bubble.
 * @param {string} message - The user's message.
 */
function createUserBubble(message, imagePreviewUrl = null) {
    window.EmiChatRender.createUserBubble(message, imagePreviewUrl, {
        chatBoxId: "chat-box",
        bubbleClass: "user-bubble",
        imageClass: "chat-image-thumb",
        scrollToBottom,
        logError: (...args) => console.error(...args),
    });
}

/**
 * Creates a new code chat bubble for displaying code snippets.
 */
function createCodeBubble() {
    console.log("Creating code bubble.");
    const chatBox = document.getElementById("chat-box");
    if (chatBox) {
        const bubble = document.createElement("div");
        const pre = document.createElement("pre");
        const code = document.createElement("code");
        code.className = "language-python"; // Adjust language as needed

        pre.appendChild(code);
        bubble.appendChild(pre);
        bubble.className = "chat-box-code-bubble";

        chatBox.appendChild(bubble);
        scrollToBottom();
        return code;
    } else {
        console.error("Chat box element with ID 'chat-box' not found.");
    }
}

/**
 * Closes the last active bot bubble by marking it as done.
 * Currently unused in the revised approach.
 */
function closeLastBubble() {
    console.log("Closing last bot bubble if exists.");
    // If you decide to use activeBotBubble, implement the logic here
}

// ==================== Data Preparation and Sending ====================

/**
 * Prepares and sends the user data to the server.
 * @param {string} message - The user's message.
 * @param {Blob|null} audioBlob - The recorded audio blob.
 */
function prepareDataToBeSent(message, audioBlob) {
    window.EmiRequestSender.prepareDataToBeSent(message, audioBlob, getEmiRequestSenderContext());
}
// Expose for external callers (e.g. geoguessr panel buttons)
window._emiSendMessage = function (text) { prepareDataToBeSent(text, null); };

// ==================== Audio Recording Functions ====================

/**
 * Toggles audio recording on or off.
 */
function toggleAudioRecording() {
    window.EmiRecorder.toggleAudioRecording(getEmiRecorderContext());
}

/**
 * Initializes and starts the MediaRecorder.
 * @param {MediaStream} stream - The audio stream from the microphone.
 */
function startRecording(stream) {
  window.EmiRecorder.startRecording(stream, getEmiRecorderContext());
}






// ==================== Text-to-Speech Functions ====================

/**
 * Enqueues a TTS audio URL for playback.
 * @param {string} audioUrl - The URL of the TTS audio.
 */
function playTTSAudio(audioUrl) {
    window.EmiAudio.playTTSAudio(audioUrl, getEmiAudioContext());
}

/**
 * Plays a sound from the given URL.
 * @param {string} soundUrl - The URL of the sound file.
 */
function playSound(soundUrl) {
    window.EmiAudio.playSound(soundUrl, getEmiAudioContext());
}

// ==================== Dedicated Widget Handlers ====================

/**
 * Handles generic widgets that don't match predefined types.
 * @param {Array<Object>} widgetDataList - An array of widget data objects.
 */
function handleGenericWidget(widgetDataList) {
    widgetDataList.forEach(widgetData => {
        console.warn("Handling generic widget:", widgetData);

        // Implement your generic widget handling logic here
        const container = document.getElementById("generic-widgets");
        if (!container) {
            console.error("Generic widgets container with ID 'generic-widgets' not found.");
            return;
        }

        const widgetElement = document.createElement("div");
        widgetElement.className = "generic-widget";
        widgetElement.textContent = JSON.stringify(widgetData, null, 2); // Display data as JSON for debugging
        container.prepend(widgetElement);
    });
}

/**
 * Handles UI audio control widgets.
 * @param {Array<Object>} widgetDataList - An array of audio control widget data objects.
 */
function handleAudioControlWidget(widgetDataList) {
    if (!Array.isArray(widgetDataList) || widgetDataList.length === 0) {
        return;
    }
    widgetDataList.forEach((widgetData) => {
        const actionRaw = widgetData?.action ?? widgetData?.data?.action;
        const action = typeof actionRaw === "string" ? actionRaw.trim().toLowerCase() : "";
        if (action === "mute") {
            setSiteMute(true);
            return;
        }
        if (action === "unmute") {
            setSiteMute(false);
            return;
        }
        if (action === "toggle") {
            setSiteMute(!mute);
            return;
        }
        console.warn("Unknown audio control action:", actionRaw);
    });
}

/**
 * Handles 'todo_task' widgets by performing a full refresh of the To-Do List.
 * @param {Array<Object>} todoDataList - An array of To-Do task data objects.
 */
function handleTodoWidget(todoDataList) {
    window.EmiTodoWidgets.handleTodoWidget(todoDataList, getEmiTodoWidgetsContext());
}

/**
 * Adds a single task to the To-Do List.
 * @param {Object} taskData - The task data object.
 */
function addTodoItem(taskData) {
    window.EmiTodoWidgets.addTodoItem(taskData, getEmiTodoWidgetsContext());
}

function attachTodoCheckboxHandlers() {
    window.EmiTodoWidgets.attachTodoCheckboxHandlers(getEmiTodoWidgetsContext());
}


/**
 * Handles 'calendar' widgets by performing a full refresh of the Calendar.
 * @param {Array<Object>} calendarDataList - An array of calendar event data objects.
 */
function handleCalendarWidget(calendarDataList) {
    const calendarList = document.getElementById("calendar-list");
    if (!calendarList) {
        console.error("Calendar list element with ID 'calendar-list' not found.");
        return;
    }

    // Clear the UI container.
    calendarList.innerHTML = "";

    // Flush the internal storage (calendarEventsByDate).
    for (const key in calendarEventsByDate) {
        if (calendarEventsByDate.hasOwnProperty(key)) {
            delete calendarEventsByDate[key];
        }
    }

    if (!Array.isArray(calendarDataList) || calendarDataList.length === 0) {
        console.warn("No calendar data provided to handleCalendarWidget.");
        calendarList.innerHTML = "<li>No calendar events.</li>";
        return;
    }

    // Repopulate the internal storage using the incoming data.
    calendarDataList.forEach(calendarData => {
        addCalendarItem(calendarData);
    });

    console.log(`Calendar refreshed with ${calendarDataList.length} event(s).`);

    // Render events for the currently selected day without changing currentCalendarDate.
    renderCalendarEvents();
}

/**
 * Adds a calendar event to the calendarEventsByDate map, ensuring no duplicates.
 * @param {Object} calendarData - The calendar event data object.
 */
function addCalendarItem(calendarData) {
    const data = calendarData.data;

    const eventTitle = data.event_name || data.summary || "No Title";

    let eventDate;
    if (typeof data.start === 'string') {
        eventDate = new Date(data.start); // New backend sends start as flat UTC ISO string
    } else {
        console.error("Calendar event 'start' field is missing or invalid:", data);
        return;
    }

    const dateKey = getFormattedDate(eventDate); // e.g. '2025-03-25'
    const eventId = data.id;

    if (!eventId) {
        console.error("Calendar event missing unique 'id':", data);
        return;
    }

    // Initialize array for the date if needed
    calendarEventsByDate[dateKey] ||= [];

    const alreadyExists = calendarEventsByDate[dateKey].some(event => event.id === eventId);
    if (alreadyExists) {
        console.log(`Duplicate calendar event "${eventTitle}" skipped for dateKey "${dateKey}"`);
        return;
    }

    calendarEventsByDate[dateKey].push(data);
    console.log(`Event "${eventTitle}" added to "${dateKey}"`);

    if (dateKey === getFormattedDate(currentCalendarDate)) {
        renderCalendarEvents(); // safe to re-render if it matches
    }
}




/**
 * Handles 'email' widgets by performing a full refresh of the Email list.
 * @param {Array<Object>} emailDataList - An array of email data objects.
 */
function handleEmailWidget(emailDataList) {
    window.EmiEmailWidgets.handleEmailWidget(emailDataList, getEmiEmailWidgetsContext());
}

/**
 * Adds an email item to the Email widget.
 * @param {Object} emailData - The email data object.
 */
function addEmailItem(emailData) {
    window.EmiEmailWidgets.addEmailItem(emailData, getEmiEmailWidgetsContext());
}


// formatValue is defined above (line ~642) — handles dates, links, arrays.


// ==================== Scheduler (Reminders) Variables ====================
let currentSchedulerDate = new Date(); // Tracks the currently displayed date
const schedulerEventsByDate = {}; // Stores scheduler events grouped by date

/**
 * Handles 'scheduler' widgets by performing a full refresh of the Scheduler (Reminders).
 * @param {Array<Object>} schedulerDataList - An array of scheduler event data objects.
 */
function handleSchedulerWidget(schedulerDataList) {
    window.EmiSchedulerWidgets.handleSchedulerWidget(schedulerDataList, getEmiSchedulerWidgetsContext());
}

function addSchedulerItem(schedulerData) {
    window.EmiSchedulerWidgets.addSchedulerItem(schedulerData, getEmiSchedulerWidgetsContext());
}

/**
 * Renders the scheduler (reminder) events for the currentSchedulerDate.
 */
function renderSchedulerEvents() {
    window.EmiSchedulerWidgets.renderSchedulerEvents(getEmiSchedulerWidgetsContext());
}

/**
 * Sets up event listeners for scheduler navigation.
 */
function setupSchedulerNavigation() {
    window.EmiSchedulerWidgets.setupSchedulerNavigation(getEmiSchedulerWidgetsContext());
}

/**
 * Renders content for generic widgets based on result_type.
 * @param {string} resultType - The type of the widget.
 * @param {Object} data - The data object for the widget.
 * @param {HTMLElement} container - The container to render content into.
 */
function renderGenericWidgetContent(resultType, data, container) {
    switch (resultType.toLowerCase()) {
        case 'weather':
            // **Correction Applied: Changed 'renderWeatherWidget' to 'updateWeatherWidget'**
            updateWeatherWidget(data); // Removed 'container' as 'updateWeatherWidget' does not accept it
            break;
        case 'news':
            renderNewsWidget(data, container);
            break;
        // Add more cases for known widget types
        default:
            // For truly dynamic or unknown types, use the generic widget item function
            addGenericWidgetItem(data, container);
            break;
    }
}

/**
 * Renders the weather data into the Weather widget.
 * @param {Object} weatherData - The weather data object.
 */
/**
 * Renders the weather data into the Weather widget.
 */
function updateWeatherWidget(weatherData) {
    window.EmiNewsWeatherWidgets.updateWeatherWidget(weatherData, getEmiNewsWeatherWidgetsContext());
}



/**
 * Renders a news widget.
 * @param {Object} data - The news data object.
 * @param {HTMLElement} container - The container to render content into.
 */
function renderNewsWidget(newsData, container) {
    window.EmiNewsWeatherWidgets.renderNewsWidget(newsData, container, getEmiNewsWeatherWidgetsContext());
}



/**
 * Renders a generic widget item with minimal and detailed views.
 * @param {Object} data - The data object for the widget.
 * @param {HTMLElement} container - The container to render content into.
 */
function addGenericWidgetItem(data, container) {
    const { title, description, ...metadata } = data; // Extract title and description

    // Clone the generic widget item template
    const template = document.getElementById("generic-widget-template");
    if (!template) {
        console.error("Generic widget item template with ID 'generic-widget-template' not found.");
        return;
    }

    const widgetItem = template.content.cloneNode(true);

    // Populate minimal view
    const minimalTitle = widgetItem.querySelector(".generic-widget__title");
    const minimalDescription = widgetItem.querySelector(".generic-widget__description");

    if (minimalTitle) {
        minimalTitle.textContent = title || 'No Title';
    } else {
        console.error(".generic-widget__title not found in template.");
    }

    if (minimalDescription) {
        minimalDescription.textContent = description || 'No Description';
    } else {
        console.error(".generic-widget__description not found in template.");
    }

    // Populate detailed view dynamically
    const detailsView = widgetItem.querySelector(".generic-widget__item--details");
    if (detailsView) {
        detailsView.classList.add("hidden"); // Hide details by default

        const excludedKeys = ['data_type', 'title', 'description']; // Exclude certain keys

        Object.entries(metadata).forEach(([key, value]) => {
            if (excludedKeys.includes(key) || !value || value === "N/A") {
                return; // Skip this key
            }

            // Format the key and value
            const formattedKey = formatKeyForDisplay(key);
            const formattedValue = formatValue(key, value);

            // Create and append the detail line
            const detailLine = document.createElement("p");
            detailLine.innerHTML = `<strong>${formattedKey}:</strong> ${formattedValue}`;
            detailsView.appendChild(detailLine);
        });
    } else {
        console.error(".generic-widget__item--details not found in template.");
    }

    // Add toggle functionality
    const minimalView = widgetItem.querySelector(".generic-widget__item--minimal");
    if (minimalView && detailsView) {
        minimalView.addEventListener("click", () => {
            detailsView.classList.toggle("hidden");
        });
    } else {
        console.error("Minimal view or details view elements not found in generic widget item.");
    }

    // Append to dynamic widgets area
    container.prepend(widgetItem); // Add to the top
}

/**
 * Capitalizes the first letter of a string.
 * @param {string} string - The string to capitalize.
 * @returns {string} - The capitalized string.
 */
function capitalizeFirstLetter(string) {
    if (!string) return '';
    return string.charAt(0).toUpperCase() + string.slice(1);
}

// ===============Ask user modal

function showAskUserModal(widgetItems) {
    if (!widgetItems || !widgetItems.length) return;
    const questionData = widgetItems[0];
    if (!questionData) return;
    const question = questionData.question || "";

    let modal = document.getElementById("ask-user-modal");
    if (!modal) {
        modal = document.createElement("div");
        modal.id = "ask-user-modal";
        modal.className = "ask-user-modal";

        modal.innerHTML = `
            <div class="ask-user-content">
                <div class="ask-user-header">
                    <span class="ask-user-icon">💬</span>
                    <span class="ask-user-label">Emi needs your input</span>
                </div>
                <div class="ask-user-body">
                    <div id="ask-user-question"></div>
                    <div class="ask-user-input-row">
                        <input id="ask-user-input" type="text" placeholder="Type your reply..." />
                        <button id="ask-user-submit">Send</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(modal);
    }

    document.getElementById("ask-user-question").innerHTML = question;
    const input = document.getElementById("ask-user-input");
    input.value = "";
    setTimeout(() => input.focus(), 100);

    const submit = async () => {
        const answer = input.value.trim();
        if (answer) {
            await sendAskUserResponse(questionData, answer);
            modal.remove();
        }
    };

    document.getElementById("ask-user-submit").onclick = submit;
    input.onkeydown = (e) => { if (e.key === "Enter") submit(); };
}

// =============== Task running notice

function handleTaskRunningNotice(widgetItems) {
    const item = widgetItems && widgetItems[0];
    if (!item) return;
    const taskId = item.task_id || "";
    const status = item.status || "";
    const waitingEvent = item.waiting_event_name || "";
    const runId = item.run_id || "";
    const taskLabel = taskId ? taskId.replace(/_/g, " ") : "task";

    let text;
    if (status === "waiting" && waitingEvent) {
        text = `▶ Task <strong>${taskLabel}</strong> started — waiting for event <code>${waitingEvent}</code>`;
    } else if (status === "completed") {
        text = `✓ Task <strong>${taskLabel}</strong> completed.`;
    } else if (status === "failed") {
        text = `✗ Task <strong>${taskLabel}</strong> failed to start.`;
    } else {
        text = `▶ Task <strong>${taskLabel}</strong> is running. <small>(${runId})</small>`;
    }

    const chatContainer = document.getElementById("chat-container") || document.querySelector(".chat-messages");
    if (!chatContainer) {
        console.log("[task_running_notice]", text);
        return;
    }
    const notice = document.createElement("div");
    notice.className = "task-running-notice";
    notice.style.cssText = "padding:6px 12px;margin:4px 0;font-size:0.85em;color:#7ec8a4;border-left:3px solid #7ec8a4;background:rgba(126,200,164,0.07);border-radius:3px;";
    notice.innerHTML = text;
    chatContainer.appendChild(notice);
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

// =============== Notify user modal

function showNotifyUserModal(widgetItems) {
    if (!widgetItems || !widgetItems.length) return;
    const notifyData = widgetItems[0];
    if (!notifyData) return;
    const message = notifyData.message || "";

    let modal = document.getElementById("notify-user-modal");
    if (!modal) {
        modal = document.createElement("div");
        modal.id = "notify-user-modal";
        modal.className = "notify-user-modal";

        modal.innerHTML = `
            <div class="notify-user-content">
                <div id="notify-user-message"></div>
                <button id="notify-user-close">OK</button>
            </div>
        `;
        document.body.appendChild(modal);
    }

    document.getElementById("notify-user-message").innerHTML = message;

    document.getElementById("notify-user-close").onclick = () => {
        modal.remove();  // ✅ Just dismisses the modal
    };
}


// ==================== Socket.IO Event Handlers ====================

/**
 * Handles incoming Socket.IO events.
 */
function setupSocketListeners() {
    // Install liveness + replay. On reconnect, include since_ts so the server
    // replays any assistant messages this client missed during the outage.
    if (window.EmiSocketHeartbeat) {
        window.EmiSocketHeartbeat.install({ socket, getRoomId: () => "master_room" });
    }
    window.EmiSocketIngress.wireSocketIngress({
        socket,
        onConnect: () => {
            console.log("Connected to the server via Socket.IO.");
            const payload = { room_id: "master_room" };
            const since = window.EmiSocketHeartbeat && window.EmiSocketHeartbeat.getLastMessageTs();
            if (since) payload.since_ts = since;
            socket.emit("register_chat_client", payload);
            console.log("💬 Registered as chat client for room_id=master_room", since ? "since=" + since : "(first connect)");
            const banner = document.getElementById("connection-status-banner");
            if (banner) banner.remove();
        },
        onDisconnect: () => {
            console.log("Disconnected from the server.");
            if (!document.getElementById("connection-status-banner")) {
                const chatBox = document.getElementById("chat-box");
                if (chatBox) {
                    const banner = document.createElement("div");
                    banner.id = "connection-status-banner";
                    banner.className = "connection-banner";
                    banner.textContent = "Connecting...";
                    chatBox.appendChild(banner);
                    scrollToBottom();
                }
            }
        },
        renderChat: (chat, meta) => {
            console.log("Rendering chat message:", chat);
            if (window.EmiSocketHeartbeat) window.EmiSocketHeartbeat.noteIncoming(chat);
            createBotBubble(chat, meta);
        },
        renderFeed: (feed) => {
            console.log("Adding feed item:", feed);
            addFeedItem(feed);
        },
        renderSound: (soundKey) => {
            if (soundKey in soundLib) {
                playSound(soundLib[soundKey]);
            } else if (soundKey === "default" || soundKey === "notification") {
                playRandomNotificationSound();
            } else {
                console.warn("Unknown sound key:", soundKey);
            }
        },
        renderWidgetGroup: (widgetType, widgetItems) => {
            console.log(`Processing widget type: ${widgetType} with ${widgetItems.length} item(s)`);
            window.EmiWidgetHandlers.dispatchWidgetGroup(widgetType, widgetItems, getEmiWidgetHandlersContext());
        },
        handleGenericWidget: (items) => handleGenericWidget(items),
        onAudioFile: (audioData) => {
            console.log(`[TTS] audio_file received. url=${audioData.audio_url} speakingMode=${speakingMode} muted=${mute}`);
            playTTSAudio(audioData.audio_url);
        },
        onRepoUpdateNotification: (updateData) => {
            console.log("Received repo update notification:", updateData);
            fetchRepoData();
        },
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    });
}

/**
 * Adds a feed item to the top of the feed list with timestamp and limits total items.
 * @param {string} text - The feed text.
 */
function addFeedItem(text) {
    window.EmiChatRender.addFeedItem(text, {
        feedListId: "feed-list",
        maxItems: 50,
        logError: (...args) => console.error(...args),
        logDebug: (...args) => console.log(...args),
    });
}


// ==================== Event Listeners ====================

/**
 * Sets up event listeners for user interactions.
 */
function setupEventListeners() {
    window.EmiComposer.bindComposer({
        formSelector: ".chat-form",
        inputId: "chat-input",
        chatBoxId: "chat-box",
        removeButtonId: "pending-image-remove",
        getSocketId: () => (socket ? socket.id : ""),
        getPendingImageFile,
        setPendingImage,
        clearPendingImage,
        createUserBubble,
        prepareDataToBeSent,
        onInputActivity: () => {
            if (window.chatbotTimeout) {
                console.log("Clearing the chatbotTimeout");
                clearTimeout(window.chatbotTimeout);
                window.chatbotTimeout = null;
            }
        },
        updatePendingImagePreviewUI,
        logDebug: (...args) => console.log(...args),
        logWarn: (...args) => console.warn(...args),
        logError: (...args) => console.error(...args),
    });

    // Header toggles TTS; icon toggles site mute
    setupTtsTitleToggle();
    setupSiteMuteToggle();

    // Toggle audio recording
    const recordBtnIcon = document.getElementById("record-btn-icon");
    if (recordBtnIcon) {
        recordBtnIcon.addEventListener('click', () => {
            const recordBtnOff = document.getElementById("record-btn-off");
            const recordBtnOn = document.getElementById("record-btn-on");
            if (recordBtnOff && recordBtnOn) {
                recordBtnOff.classList.toggle('hidden');
                recordBtnOn.classList.toggle('hidden');

                // Toggle the 'active' class based on the current recording state
                recordBtnIcon.classList.toggle('active');

                // Toggle audio recording functionality
                toggleAudioRecording();

                // Remove focus to prevent :focus styling from persisting
                recordBtnIcon.blur();
            } else {
                console.error("Record button icons with IDs 'record-btn-off' and 'record-btn-on' not found.");
            }
        });
    } else {
        console.error("Record button icon with ID 'record-btn-icon' not found.");
    }

}

// ==================== /actas chip ====================
// Persistent indicator above the chat composer showing the sticky
// principal binding (set via the `/actas self` slash command). Hidden
// when the principal is the default `"user"`. Refreshed on page load
// and whenever the chat log gets a new node (chat replies that confirm
// /actas state changes arrive as new chat-box messages).

const ACTAS_DEFAULT_PRINCIPAL = "user";

function _actasChipEls() {
    return {
        root: document.getElementById("actas-chip"),
        principalEl: document.getElementById("actas-chip__principal"),
        clearBtn: document.getElementById("actas-chip__clear"),
    };
}

function renderActasChip(principal) {
    const { root, principalEl } = _actasChipEls();
    if (!root || !principalEl) return;
    const p = String(principal || ACTAS_DEFAULT_PRINCIPAL).trim().toLowerCase();
    if (!p || p === ACTAS_DEFAULT_PRINCIPAL) {
        root.classList.add("hidden");
        principalEl.textContent = ACTAS_DEFAULT_PRINCIPAL;
        return;
    }
    principalEl.textContent = p;
    root.classList.remove("hidden");
}

async function refreshActasChip() {
    try {
        const params = new URLSearchParams({
            room_id: "master_room",
            surface: "ui",
            context_id: "main",
        });
        const res = await fetch(`/api/actas/current?${params.toString()}`);
        if (!res.ok) return;
        const data = await res.json();
        renderActasChip(data && data.principal);
    } catch (e) {
        console.warn("refreshActasChip failed", e);
    }
}

function setupActasChip() {
    const { root, clearBtn } = _actasChipEls();
    if (!root) return;
    // Clear button sends `/actas user` through the normal chat path so the
    // server-side handler runs unchanged.
    if (clearBtn) {
        clearBtn.addEventListener("click", async () => {
            const input = document.getElementById("chat-input");
            const form = document.querySelector(".chat-form");
            if (!input || !form) return;
            input.value = "/actas user";
            form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
        });
    }
    // Refresh whenever a new chat node lands. Slash replies (including
    // /actas confirmations and /end) come back as chat messages, so any
    // new node means we may need to re-read state.
    const chatBox = document.getElementById("chat-box");
    if (chatBox && "MutationObserver" in window) {
        const obs = new MutationObserver(() => { refreshActasChip(); });
        obs.observe(chatBox, { childList: true, subtree: false });
    }
    // Initial fetch.
    refreshActasChip();
}

function setupThemeListener()
{
    window.EmiMenuControls.setupThemeListener(getEmiMenuControlsContext());
}

function toggleTheme() {
    window.EmiMenuControls.toggleTheme();
}

function updateThemeIcon() {
    window.EmiMenuControls.updateThemeIcon();
}

// ===== Menu Toggle Functionality =====
function setupMenuToggle() {
    window.EmiMenuControls.setupMenuToggle(getEmiMenuControlsContext());
}

/**
 * Initializes the currentCalendarDate to the earliest date with events.
 */
function initializeCurrentCalendarDate() {
    window.EmiCalendarWidgets.initializeCurrentCalendarDate(getEmiCalendarWidgetsContext());
}

/**
 * Sets up event listeners for calendar navigation buttons.
 */
function setupCalendarNavigation() {
    window.EmiCalendarWidgets.setupCalendarNavigation(getEmiCalendarWidgetsContext());
}

/**
 * Renders the calendar events for the currentCalendarDate.
 */
function renderCalendarEvents() {
    window.EmiCalendarWidgets.renderCalendarEvents(getEmiCalendarWidgetsContext());
}

/**
 * Sends a request to a tool route with optional payload.
 * @param {string} toolName - The name of the tool (e.g., "todo", "calendar").
 * @param {string} action - The action (e.g., "update", "delete").
 * @param {Object} payload - Optional JSON body to send.
 * @returns {Promise<Object>} - The parsed JSON response.
 */
async function callToolRoute(toolName, action, payload = {}) {
    const url = /tool/;
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(payload)
        });

        if (!response.ok) {
            throw new Error(`HTTP ${response.status} - ${response.statusText}`);
        }

        const data = await response.json();
        return data;
    } catch (error) {
        console.error(`Error calling tool route: ${toolName}/${action}`, error);
        return { success: false, error: error.message };
    }
}


// ==================== Initialization ====================


document.addEventListener('DOMContentLoaded', () => {
    setupSocketListeners();
    setupEventListeners();
    attachIdleListeners();
    resetIdleTimer();
    setupIdleVisibilityHooks();
    setupThemeListener();
    setupMenuToggle(); // Initialize the hamburger menu
    setupActasChip(); // /actas principal indicator above the composer
    setupCalendarNavigation();
    initializeCurrentCalendarDate();
    renderCalendarEvents();
    setupSchedulerNavigation();
    setupChatWidgetExpansion();
    if (window.EmiTaskCreatePanel) {
        window.EmiTaskCreatePanel.init();
    }
    if (window.EmiDocPanel) {
        window.EmiDocPanel.init();
    }
    if (window.EmiGeoPanel) {
        window.EmiGeoPanel.init();
    }
    console.log("DOM fully loaded. Fetching repo data...");
    fetchRepoData();
    loadChatHistory();

    // Refresh widgets when the date changes (e.g., UI left open overnight).
    let _lastDate = new Date().toDateString();
    setInterval(() => {
        const now = new Date().toDateString();
        if (now !== _lastDate) {
            _lastDate = now;
            console.log("Date changed — resetting calendar/scheduler to today and refreshing.");
            currentCalendarDate = new Date();
            currentSchedulerDate = new Date();
            fetchRepoData();
        }
    }, 60_000);
});
function setupChatWidgetExpansion() {
    const chatWidget = document.querySelector('.widget--chat');
    if (!chatWidget) {
        console.warn('Chat widget not found.');
        return;
    }

    const expandButton = document.createElement('button');
    expandButton.id = 'expand-chat-btn';
    expandButton.innerHTML = '<i class="fas fa-expand"></i>';
    expandButton.setAttribute('aria-label', 'Expand Chat');

    const closeButton = document.createElement('button');
    closeButton.id = 'close-chat-btn';
    closeButton.innerHTML = '<i class="fas fa-times"></i>';
    closeButton.setAttribute('aria-label', 'Close Chat');
    closeButton.classList.add('hidden');

    chatWidget.appendChild(expandButton);
    chatWidget.appendChild(closeButton);

    const otherWidgets = document.querySelectorAll('.widget:not(.widget--chat)');

    expandButton.addEventListener('click', () => {
        chatWidget.classList.add('expanded');
        expandButton.classList.add('hidden');
        closeButton.classList.remove('hidden');
        otherWidgets.forEach(widget => widget.classList.add('hidden'));
    });

    closeButton.addEventListener('click', () => {
        chatWidget.classList.remove('expanded');
        expandButton.classList.remove('hidden');
        closeButton.classList.add('hidden');
        otherWidgets.forEach(widget => widget.classList.remove('hidden'));
    });
}
