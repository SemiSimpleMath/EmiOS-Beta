(function (global) {
    "use strict";

    function _renderMarkdown(text) {
        if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
            try {
                var raw = marked.parse(String(text || ""));
                return DOMPurify.sanitize(raw);
            } catch (e) {
                // Fallback to plain text if marked fails
            }
        }
        return String(text || "");
    }

    function createBotBubble(text, options) {
        const opts = options || {};
        const chatBoxId = opts.chatBoxId || "chat-box";
        const bubbleClass = opts.bubbleClass || "bot-bubble";
        const logError = typeof opts.logError === "function" ? opts.logError : console.error;
        const scrollToBottom = typeof opts.scrollToBottom === "function" ? opts.scrollToBottom : null;

        const bubble = document.createElement("div");
        const contentDiv = document.createElement("div");
        contentDiv.innerHTML = _renderMarkdown(text);
        bubble.appendChild(contentDiv);
        bubble.className = bubbleClass;

        const chatBox = document.getElementById(chatBoxId);
        if (!chatBox) {
            logError(`Chat box element with ID '${chatBoxId}' not found.`);
            return;
        }
        chatBox.appendChild(bubble);
        if (scrollToBottom) {
            scrollToBottom();
        }
    }

    function createUserBubble(message, imagePreviewUrl, options) {
        const opts = options || {};
        const chatBoxId = opts.chatBoxId || "chat-box";
        const bubbleClass = opts.bubbleClass || "user-bubble";
        const imageClass = opts.imageClass || "chat-image-thumb";
        const logError = typeof opts.logError === "function" ? opts.logError : console.error;
        const scrollToBottom = typeof opts.scrollToBottom === "function" ? opts.scrollToBottom : null;

        const bubble = document.createElement("div");
        if (message && String(message).trim() !== "") {
            const p = document.createElement("p");
            p.innerHTML = message;
            bubble.appendChild(p);
        }

        if (imagePreviewUrl) {
            const img = document.createElement("img");
            img.className = imageClass;
            img.src = imagePreviewUrl;
            img.alt = "Attached image";
            img.loading = "lazy";
            img.onload = function () {
                try {
                    URL.revokeObjectURL(imagePreviewUrl);
                } catch (_e) {
                    // best-effort cleanup
                }
            };
            bubble.appendChild(img);
        }

        bubble.className = bubbleClass;
        const chatBox = document.getElementById(chatBoxId);
        if (!chatBox) {
            logError(`Chat box element with ID '${chatBoxId}' not found.`);
            return;
        }
        chatBox.appendChild(bubble);
        if (scrollToBottom) {
            scrollToBottom();
        }
    }

    function addFeedItem(text, options) {
        const opts = options || {};
        const feedListId = opts.feedListId || "feed-list";
        const maxItems = Number.isInteger(opts.maxItems) ? opts.maxItems : 50;
        const logError = typeof opts.logError === "function" ? opts.logError : console.error;
        const logDebug = typeof opts.logDebug === "function" ? opts.logDebug : console.log;

        const feedList = document.getElementById(feedListId);
        if (!feedList) {
            logError(`Feed list element with ID '${feedListId}' not found.`);
            return;
        }

        const listItem = document.createElement("li");
        listItem.className = "feed-item";

        const timestamp = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
        const metaDiv = document.createElement("div");
        metaDiv.className = "feed__meta";
        metaDiv.innerHTML = `<span class="feed__timestamp">${timestamp}</span>`;

        const contentDiv = document.createElement("div");
        contentDiv.className = "feed__content";
        if (typeof text === "string") {
            contentDiv.innerHTML = text;
        } else {
            contentDiv.appendChild(text);
        }

        listItem.appendChild(metaDiv);
        listItem.appendChild(contentDiv);
        feedList.prepend(listItem);
        logDebug("Added feed item at the top:", text);

        while (feedList.children.length > maxItems) {
            const lastItem = feedList.lastChild;
            feedList.removeChild(lastItem);
            logDebug("Removed oldest feed item:", lastItem && lastItem.textContent);
        }
    }

    global.EmiChatRender = {
        createBotBubble: createBotBubble,
        createUserBubble: createUserBubble,
        addFeedItem: addFeedItem,
    };
})(window);
