(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getAudioChunks: typeof ctx?.getAudioChunks === "function" ? ctx.getAudioChunks : function () { return []; },
            setAudioChunks: typeof ctx?.setAudioChunks === "function" ? ctx.setAudioChunks : function () {},
            getIsRecording: typeof ctx?.getIsRecording === "function" ? ctx.getIsRecording : function () { return false; },
            setIsRecording: typeof ctx?.setIsRecording === "function" ? ctx.setIsRecording : function () {},
            getMediaRecorder: typeof ctx?.getMediaRecorder === "function" ? ctx.getMediaRecorder : function () { return null; },
            setMediaRecorder: typeof ctx?.setMediaRecorder === "function" ? ctx.setMediaRecorder : function () {},
            getMediaStream: typeof ctx?.getMediaStream === "function" ? ctx.getMediaStream : function () { return null; },
            setMediaStream: typeof ctx?.setMediaStream === "function" ? ctx.setMediaStream : function () {},
            createUserBubble: typeof ctx?.createUserBubble === "function" ? ctx.createUserBubble : function () {},
            prepareDataToBeSent: typeof ctx?.prepareDataToBeSent === "function" ? ctx.prepareDataToBeSent : function () {},
            getMessageInput: typeof ctx?.getMessageInput === "function" ? ctx.getMessageInput : function () { return null; },
            alertUser: typeof ctx?.alertUser === "function" ? ctx.alertUser : function (message) { alert(message); },
            resetRecordingUI: typeof ctx?.resetRecordingUI === "function" ? ctx.resetRecordingUI : function () {},
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    // 25 MB — well within Cloudflare's 100 MB limit but stops runaway recordings
    var MAX_BLOB_BYTES = 25 * 1024 * 1024;
    // 3 minutes max recording duration
    var MAX_RECORD_MS = 3 * 60 * 1000;

    function _stopStreamTracks(stream) {
        if (!stream) return;
        try {
            stream.getTracks().forEach(function (track) { track.stop(); });
        } catch (_e) {}
    }

    function startRecording(stream, ctx) {
        const fns = _getFns(ctx);
        fns.setAudioChunks([]);

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

        const opts = mimeType ? { mimeType: mimeType } : {};
        let mediaRecorder = null;
        try {
            mediaRecorder = new MediaRecorder(stream, opts);
        } catch (err) {
            fns.logWarn("MediaRecorder fallback init", err);
            mediaRecorder = new MediaRecorder(stream);
        }
        fns.setMediaRecorder(mediaRecorder);
        fns.logDebug("Recording started with", mediaRecorder.mimeType);

        // Auto-stop after MAX_RECORD_MS to prevent runaway recordings
        var autoStopTimer = setTimeout(function () {
            if (mediaRecorder && mediaRecorder.state === "recording") {
                fns.logWarn("Max recording duration reached — auto-stopping.");
                mediaRecorder.stop();
                fns.setIsRecording(false);
                fns.alertUser("Recording stopped automatically after 3 minutes.");
            }
        }, MAX_RECORD_MS);

        mediaRecorder.ondataavailable = function (e) {
            if (e.data && e.data.size > 0) {
                const chunks = fns.getAudioChunks();
                chunks.push(e.data);
                fns.setAudioChunks(chunks);
            }
        };

        mediaRecorder.onstop = function () {
            clearTimeout(autoStopTimer);
            const chunks = fns.getAudioChunks();
            fns.logDebug("Recording stopped; chunks:", chunks.length);
            const blob = new Blob(chunks, { type: mediaRecorder.mimeType });
            fns.logDebug("Blob size (bytes):", blob.size);

            // Release the microphone immediately — stops the orange iOS indicator.
            _stopStreamTracks(fns.getMediaStream());
            fns.setMediaStream(null);

            if (blob.size > MAX_BLOB_BYTES) {
                fns.logError("Recording too large:", blob.size, "bytes (limit:", MAX_BLOB_BYTES, ")");
                fns.resetRecordingUI();
                fns.alertUser("Recording is too long. Please keep voice messages under 3 minutes.");
                return;
            }

            const test = new Audio(URL.createObjectURL(blob));
            test.onloadedmetadata = function () {
                fns.logDebug("Blob duration:", test.duration, "s");
            };

            const input = fns.getMessageInput();
            const msg = input && typeof input.value === "string" ? input.value.trim() : "";
            if (msg) {
                fns.createUserBubble(msg);
                fns.prepareDataToBeSent(msg, blob);
                input.value = "";
            } else {
                fns.prepareDataToBeSent("", blob);
            }
        };

        if (isIOS) {
            mediaRecorder.start();
        } else {
            mediaRecorder.start(250);
        }
        fns.setIsRecording(true);
    }

    function toggleAudioRecording(ctx) {
        const fns = _getFns(ctx);
        const mediaRecorder = fns.getMediaRecorder();
        if (mediaRecorder && mediaRecorder.state === "recording") {
            mediaRecorder.stop();
            fns.setIsRecording(false);
            fns.logDebug("Recording stopped.");
            // Stream tracks released in onstop handler.
            return;
        }

        // Always request a fresh stream — ensures microphone is only held while recording.
        navigator.mediaDevices.getUserMedia({ audio: true })
            .then(function (stream) {
                fns.setMediaStream(stream);
                startRecording(stream, ctx);
            })
            .catch(function (error) {
                fns.logError("Error accessing microphone:", error);
                fns.alertUser("Microphone access is required to record audio.");
            });
    }

    global.EmiRecorder = {
        toggleAudioRecording: toggleAudioRecording,
        startRecording: startRecording,
    };
})(window);
