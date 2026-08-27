(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getChatMode: typeof ctx?.getChatMode === "function" ? ctx.getChatMode : function () { return "normal"; },
            setChatMode: typeof ctx?.setChatMode === "function" ? ctx.setChatMode : function () {},
            updateModeIndicator: typeof ctx?.updateModeIndicator === "function" ? ctx.updateModeIndicator : function () {},
            showModeNotification: typeof ctx?.showModeNotification === "function" ? ctx.showModeNotification : function () {},
            getSocketId: typeof ctx?.getSocketId === "function" ? ctx.getSocketId : function () { return ""; },
            getPendingImageFile: typeof ctx?.getPendingImageFile === "function" ? ctx.getPendingImageFile : function () { return null; },
            clearPendingImage: typeof ctx?.clearPendingImage === "function" ? ctx.clearPendingImage : function () {},
            getLastInteractionTime: typeof ctx?.getLastInteractionTime === "function" ? ctx.getLastInteractionTime : function () { return 0; },
            setLastInteractionTime: typeof ctx?.setLastInteractionTime === "function" ? ctx.setLastInteractionTime : function () {},
            getSpeakingMode: typeof ctx?.getSpeakingMode === "function" ? ctx.getSpeakingMode : function () { return false; },
            createUserBubble: typeof ctx?.createUserBubble === "function" ? ctx.createUserBubble : function () {},
            sendAgain: typeof ctx?.sendAgain === "function" ? ctx.sendAgain : function () {},
            clearAudioChunks: typeof ctx?.clearAudioChunks === "function" ? ctx.clearAudioChunks : function () {},
            resetRecordingUI: typeof ctx?.resetRecordingUI === "function" ? ctx.resetRecordingUI : function () {},
            alertUser: typeof ctx?.alertUser === "function" ? ctx.alertUser : function (message) { alert(message); },
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    // ---------------------------------------------------------------------------
    // Help text — purely informational, rendered locally.
    // The actual command handling lives on the server (room_ingress_service.py).
    // ---------------------------------------------------------------------------
    const SLASH_COMMANDS = [
        { cmd: "/help",                       desc: "Show this list of commands." },
        { cmd: "/music",                      desc: "Open the music page." },
        { cmd: "/music play <query>",         desc: "Play music by search query (no master-room chat)." },
        { cmd: "/music pause|next|previous",  desc: "Control playback directly." },
        { cmd: "/task create",                desc: "Open the task editor with a blank template." },
        { cmd: "/task load &lt;name&gt;",     desc: "Load an existing task spec by name. Omit name to list all." },
        { cmd: "/doc [md|gdoc] [name]",       desc: "Create a new document (markdown or Google Doc)." },
        { cmd: "/doc load &lt;name&gt;",      desc: "Load a local document by name." },
        { cmd: "/doc load gdoc &lt;name&gt;", desc: "Load an existing Google Doc by name from Drive." },
        { cmd: "/play geoguessr",             desc: "Start a GeoGuessr game in the side panel." },
        { cmd: "/play pause",                 desc: "Pause the current GeoGuessr round." },
        { cmd: "/play resume",                desc: "Resume a paused GeoGuessr round." },
        { cmd: "/play next",                  desc: "Start the next GeoGuessr round." },
        { cmd: "/play stop",                  desc: "Close the GeoGuessr panel." },
        { cmd: "/done",                       desc: "End the current session (doc, task, or plan mode). Alias: /end" },
        { cmd: "/memo",                       desc: "Toggle memo mode (log message without sending to Emi)." },
        { cmd: "/test",                       desc: "Toggle test mode." },
        { cmd: "/normal",                     desc: "Return to normal chat mode." },
    ];

    function _showHelp() {
        const rows = SLASH_COMMANDS.map(function (c) {
            return "<tr><td style='padding:2px 12px 2px 0;white-space:nowrap;font-family:monospace;color:var(--accent,#7eb8f7)'>"
                + c.cmd + "</td><td style='padding:2px 0;color:var(--text-secondary,#ccc)'>" + c.desc + "</td></tr>";
        }).join("");
        const html = "<strong>Available commands</strong><br><br><table style='border-collapse:collapse'>" + rows + "</table>";
        if (window.EmiChatRender && typeof window.EmiChatRender.createBotBubble === "function") {
            window.EmiChatRender.createBotBubble(html);
        }
    }

    // ---------------------------------------------------------------------------
    // Local-only commands — pure UI state, no server involvement needed.
    // Everything else is sent to the server as-is.
    // ---------------------------------------------------------------------------
    var _LOCAL_COMMANDS = [
        {
            pattern: /^\/music\b/i,
            handle: function (match, fns) {
                const raw = (match && match.input ? String(match.input) : "").trim();
                const rest = raw.replace(/^\/music\b/i, "").trim();
                const lowered = rest.toLowerCase();

                // Bare `/music` opens the music page directly.
                if (!rest) {
                    window.location.href = "/music";
                    return;
                }

                function runMusicCommand(command, payload) {
                    return fetch("/api/music/dj/command", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            command: command,
                            payload: payload || {},
                        }),
                    }).then(function (response) {
                        if (!response.ok) {
                            throw new Error("Music command failed with HTTP " + response.status);
                        }
                        return response.json();
                    });
                }

                function runSimpleEndpoint(path) {
                    return fetch(path, { method: "POST" }).then(function (response) {
                        if (!response.ok) {
                            throw new Error("Music endpoint failed with HTTP " + response.status);
                        }
                        return response.json();
                    });
                }

                let request;
                if (lowered === "pause") {
                    request = runMusicCommand("pause", {});
                } else if (lowered === "next") {
                    request = runMusicCommand("next", {});
                } else if (lowered === "previous" || lowered === "prev") {
                    request = runMusicCommand("previous", {});
                } else if (lowered === "enable") {
                    request = runSimpleEndpoint("/api/music/dj/enable");
                } else if (lowered === "disable") {
                    request = runSimpleEndpoint("/api/music/dj/disable");
                } else if (lowered === "pick" || lowered === "pick_once") {
                    request = runSimpleEndpoint("/api/music/dj/pick_once");
                } else if (lowered.startsWith("play ")) {
                    const query = rest.slice(5).trim();
                    if (!query) {
                        fns.showModeNotification("Music command requires a query after /music play");
                        return;
                    }
                    request = runMusicCommand("search_and_play", { query: query });
                } else {
                    // Default fallback for `/music <anything>` is a direct song search.
                    request = runMusicCommand("search_and_play", { query: rest });
                }

                request
                    .then(function () {
                        fns.showModeNotification("Music command sent");
                    })
                    .catch(function (error) {
                        fns.logError("Music slash command failed:", error);
                        fns.alertUser("Music command failed. " + (error && error.message ? error.message : ""));
                    });
            },
        },
        {
            pattern: /^\/help\b/i,
            handle: function () { _showHelp(); },
        },
        {
            pattern: /^\/test\b/i,
            handle: function (_m, fns) {
                const next = fns.getChatMode() === "test" ? "normal" : "test";
                fns.setChatMode(next);
                fns.updateModeIndicator();
                fns.showModeNotification("Test mode " + (next === "test" ? "ON" : "OFF"));
            },
        },
        {
            pattern: /^\/memo\b/i,
            handle: function (_m, fns) {
                const next = fns.getChatMode() === "memo" ? "normal" : "memo";
                fns.setChatMode(next);
                fns.updateModeIndicator();
                fns.showModeNotification("Memo mode " + (next === "memo" ? "ON" : "OFF"));
            },
        },
        {
            pattern: /^\/normal\b/i,
            handle: function (_m, fns) {
                fns.setChatMode("normal");
                fns.updateModeIndicator();
                fns.showModeNotification("Normal mode ON");
            },
        },
    ];

    function parseCommand(message, ctx) {
        const fns = _getFns(ctx);
        const text = (message || "").trim();

        if (!text.startsWith("/")) {
            return { mode: "normal", text: message };
        }

        // Check local-only commands first — these never go to the server.
        for (var i = 0; i < _LOCAL_COMMANDS.length; i++) {
            var route = _LOCAL_COMMANDS[i];
            var match = route.pattern.exec(text);
            if (match) {
                route.handle(match, fns);
                return { mode: "command", text: message };
            }
        }

        // Everything else — including all session/panel slash commands — goes to
        // the server. Routing and panel-open directives are the server's responsibility.
        return { mode: "normal", text: message };
    }

    function prepareDataToBeSent(message, audioBlob, ctx) {
        const fns = _getFns(ctx);
        fns.logDebug("In prepareDataToBeSent");

        const parsed = parseCommand(message, ctx);
        if (parsed.mode !== "normal" && parsed.text === message) {
            fns.clearAudioChunks();
            return;
        }

        const formData = new FormData();
        if (audioBlob) {
            formData.append("audio", audioBlob, "recording.webm");
        }
        formData.append("text", parsed.text);

        const pendingImageFile = fns.getPendingImageFile();
        const imageToSend = (!audioBlob && pendingImageFile) ? pendingImageFile : null;
        const hadImage = !!imageToSend;
        if (imageToSend) {
            formData.append("image", imageToSend, imageToSend.name || "image");
        }

        const chatMode = fns.getChatMode();
        if (chatMode === "test") {
            formData.append("test", "true");
        } else if (chatMode === "memo") {
            formData.append("memo", "true");
        }

        let elapsedSeconds = 0;
        const lastInteractionTime = fns.getLastInteractionTime();
        if (lastInteractionTime !== 0) {
            const currentTime = Date.now();
            elapsedSeconds = Math.floor((currentTime - lastInteractionTime) / 1000);
            fns.setLastInteractionTime(0);
        }
        formData.append("elapsed_time", elapsedSeconds);
        formData.append("speaking_mode", fns.getSpeakingMode());

        // Claim the conversation for THIS window. The server binds room -> socket
        // for whoever just talked, so the reply comes back here even if this
        // window had gone passive (swept while idle, or displaced by another
        // window). Without this the send and the reply travel on unrelated
        // channels: the POST succeeds while the reply is dropped as RoomNotBound.
        const hb = window.EmiSocketHeartbeat;
        if (hb) {
            const socketId = hb.getSocketId();
            const roomId = hb.getRoomId();
            if (socketId) formData.append("socket_id", socketId);
            if (roomId) formData.append("room_id", roomId);
            // We are now the most recent window to talk, so we own the room —
            // clear any passive banner and resume liveness probes.
            hb.noteTalked();
        }

        const route = audioBlob ? "/process_audio" : "/process_request";
        fetch(route, {
            method: "POST",
            body: formData,
        })
            .then(async function (response) {
                let data = {};
                try {
                    data = await response.json();
                } catch (e) {
                    fns.logDebug("Response had no JSON body", e);
                }
                if (!response.ok) {
                    const errMsg = (data && (data.error || data.message))
                        ? (data.error || data.message)
                        : ("HTTP " + response.status);
                    throw new Error(errMsg);
                }
                return data;
            })
            .then(function (data) {
                if (data.transcribed_text) {
                    fns.logDebug("Transcribed Text Received:", data.transcribed_text);
                    fns.createUserBubble(data.transcribed_text);
                    fns.sendAgain(data.transcribed_text, null);
                } else {
                    fns.logDebug("Data received:", data);
                }

                if (hadImage) {
                    fns.clearPendingImage();
                }
            })
            .catch(function (error) {
                fns.logError("Error sending data:", error);
                fns.resetRecordingUI();
                const messageText = "An error occurred while sending your message. " + (error && error.message ? error.message : "");
                fns.alertUser(messageText.trim());
            });

        fns.clearAudioChunks();
    }

    global.EmiRequestSender = {
        parseCommand: parseCommand,
        prepareDataToBeSent: prepareDataToBeSent,
        SLASH_COMMANDS: SLASH_COMMANDS,
    };
})(window);
