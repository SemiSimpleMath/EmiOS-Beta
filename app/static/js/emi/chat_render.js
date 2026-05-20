(function (global) {
    "use strict";

    // Canonical pod URI shape: datapod:<kind>:<id>
    // Mirrors POD_URI_RE in app/assistant/pod_store/pod_uri.py — keep
    // these two in sync if either side changes.
    var _POD_URI_RE = /\bdatapod:[a-z_][a-z_0-9]*:[a-z0-9]{6,}\b/g;
    var _IMAGE_POD_URI_RE = /\bdatapod:image:[a-z0-9]{6,}\b/g;

    function _expandImagePods(html) {
        // After markdown rendering + sanitization, image-pod URIs may
        // appear as plain text in the chat bubble. Replace any image-pod
        // URI in TEXT NODES (not inside attribute values) with an inline
        // <img> tag pointing at /api/pods/<id>/image so the user
        // actually sees the image content. Non-image pod URIs
        // (chat_cluster, email, etc.) are left as plain text.
        //
        // Walking text nodes (rather than running a regex over the HTML
        // string) avoids breaking the case where the LLM wrapped the
        // URI as a markdown link like [photo](datapod:image:abc) —
        // marked turns that into <a href="datapod:image:abc">photo</a>
        // and a string-level regex would mangle the href.
        try {
            var doc = new DOMParser().parseFromString(
                '<div id="__expand_root__">' + String(html || "") + "</div>",
                "text/html"
            );
            var root = doc.getElementById("__expand_root__");
            if (!root) return html;
            _expandInNode(root);
            return root.innerHTML;
        } catch (_e) {
            return html;
        }
    }

    function _expandInNode(node) {
        // Recursively walk children. For each text node, scan for
        // image-pod URIs and replace each occurrence with an <a><img></a>
        // fragment. Element children recurse into themselves.
        var children = Array.from(node.childNodes);
        for (var i = 0; i < children.length; i++) {
            var child = children[i];
            if (child.nodeType === Node.TEXT_NODE) {
                _expandTextNode(child);
            } else if (child.nodeType === Node.ELEMENT_NODE) {
                _expandInNode(child);
            }
        }
    }

    function _expandTextNode(textNode) {
        var text = textNode.nodeValue || "";
        if (text.indexOf("datapod:image:") < 0) return;
        // Reset regex state (it has /g flag).
        _IMAGE_POD_URI_RE.lastIndex = 0;
        var match;
        var pieces = [];
        var lastEnd = 0;
        while ((match = _IMAGE_POD_URI_RE.exec(text)) !== null) {
            if (match.index > lastEnd) {
                pieces.push(document.createTextNode(text.slice(lastEnd, match.index)));
            }
            var uri = match[0];
            var src = "/api/pods/" + encodeURIComponent(uri) + "/image";
            var a = document.createElement("a");
            a.href = src;
            a.target = "_blank";
            a.rel = "noopener";
            a.className = "chat-pod-image-link";
            var img = document.createElement("img");
            img.src = src;
            img.alt = uri;
            img.loading = "lazy";
            img.className = "chat-pod-image";
            a.appendChild(img);
            pieces.push(a);
            lastEnd = match.index + uri.length;
        }
        if (lastEnd === 0) return;  // no matches
        if (lastEnd < text.length) {
            pieces.push(document.createTextNode(text.slice(lastEnd)));
        }
        var parent = textNode.parentNode;
        if (!parent) return;
        for (var j = 0; j < pieces.length; j++) {
            parent.insertBefore(pieces[j], textNode);
        }
        parent.removeChild(textNode);
    }

    function _renderMarkdown(text) {
        if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
            try {
                var raw = marked.parse(String(text || ""));
                var sanitized = DOMPurify.sanitize(raw);
                return _expandImagePods(sanitized);
            } catch (e) {
                // Fallback to plain text if marked fails
            }
        }
        // No marked? Still try to expand image pods on the raw text.
        return _expandImagePods(String(text || ""));
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
