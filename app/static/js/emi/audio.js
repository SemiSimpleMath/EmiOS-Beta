(function (global) {
    "use strict";

    function _getFns(ctx) {
        if (!ctx || typeof ctx !== "object") {
            throw new Error("EmiAudio requires a context object.");
        }
        return {
            getMute: ctx.getMute,
            setMute: ctx.setMute,
            getSpeakingMode: ctx.getSpeakingMode,
            setSpeakingMode: ctx.setSpeakingMode,
            getAudioQueue: ctx.getAudioQueue,
            setAudioQueue: ctx.setAudioQueue,
            getIsAudioPlaying: ctx.getIsAudioPlaying,
            setIsAudioPlaying: ctx.setIsAudioPlaying,
            getTtsAudioQueue: ctx.getTtsAudioQueue,
            setTtsAudioQueue: ctx.setTtsAudioQueue,
            getIsTtsPlaying: ctx.getIsTtsPlaying,
            setIsTtsPlaying: ctx.setIsTtsPlaying,
            getAudioPlayer: ctx.getAudioPlayer,
            syncSiteMuteUi: ctx.syncSiteMuteUi,
            syncTtsTitleUi: ctx.syncTtsTitleUi,
            updateSpeakModeUi: ctx.updateSpeakModeUi,
            playNextAudio: ctx.playNextAudio,
            playNextTTSAudio: ctx.playNextTTSAudio,
            logWarn: typeof ctx.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx.logError === "function" ? ctx.logError : console.error,
        };
    }

    function stopAllAudioPlayback(ctx) {
        const f = _getFns(ctx);
        f.setAudioQueue([]);
        f.setIsAudioPlaying(false);
        f.setTtsAudioQueue([]);
        f.setIsTtsPlaying(false);

        const audioPlayer = f.getAudioPlayer();
        if (audioPlayer) {
            try {
                audioPlayer.pause();
                audioPlayer.currentTime = 0;
                audioPlayer.src = "";
            } catch (e) {
                f.logWarn("Failed to stop audio", e);
            }
        }

        const notificationAudio = document.getElementById("notificationSound");
        if (notificationAudio) {
            try {
                notificationAudio.pause();
                notificationAudio.currentTime = 0;
            } catch (_e) {
                // best-effort
            }
        }
    }

    function setSpeakingMode(isOn, ctx) {
        const f = _getFns(ctx);
        const nextValue = !!isOn;
        f.setSpeakingMode(nextValue);
        if (typeof f.updateSpeakModeUi === "function") f.updateSpeakModeUi(nextValue);
        if (typeof f.syncTtsTitleUi === "function") f.syncTtsTitleUi();

        if (!nextValue) {
            f.setTtsAudioQueue([]);
            f.setIsTtsPlaying(false);
            const audioPlayer = f.getAudioPlayer();
            if (audioPlayer) {
                try {
                    audioPlayer.pause();
                    audioPlayer.src = "";
                } catch (e) {
                    f.logWarn("Failed to stop TTS audio", e);
                }
            }
        }
    }

    function setSiteMute(isMuted, ctx) {
        const f = _getFns(ctx);
        f.setMute(!!isMuted);
        if (typeof f.syncSiteMuteUi === "function") f.syncSiteMuteUi();
        if (f.getMute()) {
            stopAllAudioPlayback(ctx);
        }
    }

    function playNextAudio(ctx) {
        const f = _getFns(ctx);
        const queue = f.getAudioQueue();
        if (!Array.isArray(queue) || queue.length === 0) return;

        if (f.getMute()) {
            f.setAudioQueue([]);
            f.setIsAudioPlaying(false);
            return;
        }

        const audioUrl = queue.shift();
        const audio = document.getElementById("chat-bot-audio");
        if (!audio) {
            f.logError("Audio element with ID 'chat-bot-audio' not found.");
            return;
        }
        audio.src = audioUrl;
        audio.play()
            .then(() => {
                f.setIsAudioPlaying(true);
            })
            .catch((error) => {
                f.logError("Error playing audio:", error);
            });

        audio.onended = function () {
            f.setIsAudioPlaying(false);
            if (typeof f.playNextAudio === "function") {
                f.playNextAudio();
            }
        };
    }

    function playNextTTSAudio(ctx) {
        const f = _getFns(ctx);
        const queue = f.getTtsAudioQueue();
        if (!Array.isArray(queue) || queue.length === 0) return;

        if (f.getMute() || !f.getSpeakingMode()) {
            f.setIsTtsPlaying(false);
            return;
        }

        f.setIsTtsPlaying(true);
        const audioUrl = queue.shift();
        const audioPlayer = f.getAudioPlayer();
        if (!audioPlayer) {
            f.logError("Audio player not initialized for TTS.");
            f.setIsTtsPlaying(false);
            return;
        }
        audioPlayer.src = audioUrl;
        audioPlayer.play()
            .catch((error) => {
                f.logError("Error playing TTS audio:", error);
                f.setIsTtsPlaying(false);
            });

        audioPlayer.onended = function () {
            f.setIsTtsPlaying(false);
            if (typeof f.playNextTTSAudio === "function") {
                f.playNextTTSAudio();
            }
        };
    }

    function playTTSAudio(audioUrl, ctx) {
        const f = _getFns(ctx);
        if (f.getMute() || !f.getSpeakingMode()) return;
        const queue = f.getTtsAudioQueue();
        if (!Array.isArray(queue)) {
            f.setTtsAudioQueue([audioUrl]);
        } else {
            queue.push(audioUrl);
        }
        if (!f.getIsTtsPlaying() && typeof f.playNextTTSAudio === "function") {
            f.playNextTTSAudio();
        }
    }

    function playSound(soundUrl, ctx) {
        const f = _getFns(ctx);
        if (f.getMute()) return;
        if (!soundUrl || typeof soundUrl !== "string" || soundUrl.trim() === "") {
            f.logWarn("No valid sound URL provided. Skipping audio playback.");
            return;
        }
        const audioPlayer = f.getAudioPlayer();
        if (!audioPlayer) {
            f.logError("Audio player not initialized for sound playback.");
            return;
        }
        audioPlayer.src = soundUrl;
        audioPlayer.volume = 1.2;
        audioPlayer.play().catch((e) => {
            f.logError("Error playing sound:", e);
        });
    }

    global.EmiAudio = {
        stopAllAudioPlayback: stopAllAudioPlayback,
        setSpeakingMode: setSpeakingMode,
        setSiteMute: setSiteMute,
        playNextAudio: playNextAudio,
        playNextTTSAudio: playNextTTSAudio,
        playTTSAudio: playTTSAudio,
        playSound: playSound,
    };
})(window);
