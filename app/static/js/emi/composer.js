(function (global) {
    "use strict";

    function bindComposer(options) {
        const opts = options || {};
        const formSelector = opts.formSelector || ".chat-form";
        const inputId = opts.inputId || "chat-input";
        const chatBoxId = opts.chatBoxId || "chat-box";
        const removeButtonId = opts.removeButtonId || "pending-image-remove";
        const logError = typeof opts.logError === "function" ? opts.logError : console.error;
        const logWarn = typeof opts.logWarn === "function" ? opts.logWarn : console.warn;
        const logDebug = typeof opts.logDebug === "function" ? opts.logDebug : console.log;

        const chatForm = document.querySelector(formSelector);
        const messageInput = document.getElementById(inputId);
        if (!chatForm) {
            logError(`Chat form element with selector '${formSelector}' not found.`);
            return;
        }
        if (!messageInput) {
            logWarn(`Message input element with ID '${inputId}' not found.`);
            return;
        }

        if (chatForm.dataset.boundComposer === "true") {
            return;
        }
        chatForm.dataset.boundComposer = "true";

        const getPendingImageFile = typeof opts.getPendingImageFile === "function"
            ? opts.getPendingImageFile
            : function () { return null; };
        const setPendingImage = typeof opts.setPendingImage === "function"
            ? opts.setPendingImage
            : function () {};
        const clearPendingImage = typeof opts.clearPendingImage === "function"
            ? opts.clearPendingImage
            : function () {};
        const createUserBubble = typeof opts.createUserBubble === "function"
            ? opts.createUserBubble
            : function () {};
        const prepareDataToBeSent = typeof opts.prepareDataToBeSent === "function"
            ? opts.prepareDataToBeSent
            : function () {};
        const onInputActivity = typeof opts.onInputActivity === "function"
            ? opts.onInputActivity
            : function () {};
        const updatePendingImagePreviewUI = typeof opts.updatePendingImagePreviewUI === "function"
            ? opts.updatePendingImagePreviewUI
            : function () {};
        const getSocketId = typeof opts.getSocketId === "function"
            ? opts.getSocketId
            : function () { return ""; };

        function submitCurrentMessage() {
            const sid = getSocketId();
            if (!sid) {
                logWarn("Cannot send — socket not connected yet.");
                return;
            }

            const message = (messageInput.value || "").trim();
            const pendingImageFile = getPendingImageFile();
            const canSend = message !== "" || !!pendingImageFile;
            if (!canSend) return;

            const bubbleImageUrl = pendingImageFile ? URL.createObjectURL(pendingImageFile) : null;
            createUserBubble(message, bubbleImageUrl);
            prepareDataToBeSent(message, null);
            messageInput.value = "";
        }

        chatForm.addEventListener("submit", function (event) {
            event.preventDefault();
            logDebug("Chat form submitted.");
            submitCurrentMessage();
        });

        messageInput.addEventListener("paste", function (event) {
            const items = event.clipboardData && event.clipboardData.items;
            if (!items) return;
            for (const item of items) {
                if (item && item.kind === "file") {
                    const file = item.getAsFile();
                    if (file && (file.type || "").startsWith("image/")) {
                        setPendingImage(file);
                        event.preventDefault();
                        break;
                    }
                }
            }
        });

        messageInput.addEventListener("keydown", function (event) {
            if (event.key !== "Enter") return;
            if (event.shiftKey) return;
            event.preventDefault();
            submitCurrentMessage();
        });

        messageInput.addEventListener("input", function () {
            onInputActivity();
        });

        const chatBox = document.getElementById(chatBoxId);
        const dropTargets = [chatBox, messageInput].filter(Boolean);
        dropTargets.forEach(function (el) {
            el.addEventListener("dragover", function (event) {
                event.preventDefault();
            });
            el.addEventListener("drop", function (event) {
                event.preventDefault();
                const files = event.dataTransfer && event.dataTransfer.files;
                if (!files || files.length === 0) return;
                const file = Array.from(files).find(function (f) {
                    return (f.type || "").startsWith("image/");
                });
                if (file) setPendingImage(file);
            });
        });

        const removeBtn = document.getElementById(removeButtonId);
        if (removeBtn) {
            removeBtn.addEventListener("click", function () {
                clearPendingImage();
            });
        }
        updatePendingImagePreviewUI();
    }

    global.EmiComposer = {
        bindComposer: bindComposer,
    };
})(window);
