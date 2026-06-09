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

    // --- Non-image pod links → click-to-view modal --------------------
    // Image pods become inline <img> (above). Every OTHER pod URI (e.g.
    // research_finding) is wrapped in a clickable link that opens a viewer
    // fetching /api/pods/<id>. Only kinds the server allowlists return
    // content; anything else shows a "not available" message.

    function _linkifyPods(html) {
        try {
            var doc = new DOMParser().parseFromString(
                '<div id="__pod_link_root__">' + String(html || "") + "</div>",
                "text/html"
            );
            var root = doc.getElementById("__pod_link_root__");
            if (!root) return html;
            _linkifyInNode(root);
            return root.innerHTML;
        } catch (_e) {
            return html;
        }
    }

    function _linkifyInNode(node) {
        var children = Array.from(node.childNodes);
        for (var i = 0; i < children.length; i++) {
            var child = children[i];
            if (child.nodeType === Node.TEXT_NODE) {
                _linkifyTextNode(child);
            } else if (child.nodeType === Node.ELEMENT_NODE) {
                if (child.tagName === "A") continue;  // never nest a pod link inside a link
                _linkifyInNode(child);
            }
        }
    }

    function _linkifyTextNode(textNode) {
        var text = textNode.nodeValue || "";
        if (text.indexOf("datapod:") < 0) return;
        // Create nodes in the text node's OWN document (the DOMParser doc),
        // not the global document — cross-document insertBefore is unreliable.
        var d = textNode.ownerDocument || document;
        _POD_URI_RE.lastIndex = 0;
        var match, pieces = [], lastEnd = 0;
        while ((match = _POD_URI_RE.exec(text)) !== null) {
            var uri = match[0];
            if (uri.indexOf("datapod:image:") === 0) continue;  // images handled by _expandImagePods
            if (match.index > lastEnd) {
                pieces.push(d.createTextNode(text.slice(lastEnd, match.index)));
            }
            var a = d.createElement("a");
            a.href = "#";
            a.className = "chat-pod-link";
            a.setAttribute("data-pod-id", uri);
            a.title = "Open saved finding";
            var code = d.createElement("code");
            code.textContent = uri;
            a.appendChild(code);
            pieces.push(a);
            lastEnd = match.index + uri.length;
        }
        if (lastEnd === 0) return;  // only image (or no) matches — leave as-is
        if (lastEnd < text.length) pieces.push(document.createTextNode(text.slice(lastEnd)));
        var parent = textNode.parentNode;
        if (!parent) return;
        for (var j = 0; j < pieces.length; j++) parent.insertBefore(pieces[j], textNode);
        parent.removeChild(textNode);
    }

    function _wirePodLinks(container) {
        // Attach the click handler DIRECTLY to each pod link in the live DOM
        // (the linkify step builds the markup inside a detached document, so a
        // delegated listener and the data-pod-id attribute both proved unreliable).
        if (!container || !container.querySelectorAll) return;
        var links = container.querySelectorAll(".chat-pod-link");
        for (var i = 0; i < links.length; i++) {
            (function (a) {
                if (a._podWired) return;
                a._podWired = true;
                a.addEventListener("click", function (e) {
                    e.preventDefault();
                    // id is in the attribute; use the visible <code> text if it's missing
                    var id = (a.getAttribute("data-pod-id") || a.textContent || "").trim();
                    if (id) _openPodViewer(id);
                });
            })(links[i]);
        }
    }

    function _podModal() {
        var modal = document.getElementById("pod-viewer-modal");
        if (modal) return modal;
        modal = document.createElement("div");
        modal.id = "pod-viewer-modal";
        modal.className = "pod-viewer-overlay";
        modal.style.display = "none";
        modal.innerHTML =
            '<div class="pod-viewer-card" role="dialog" aria-modal="true">' +
            '  <button class="pod-viewer-close" aria-label="Close">×</button>' +
            '  <div class="pod-viewer-title"></div>' +
            '  <div class="pod-viewer-meta"></div>' +
            '  <div class="pod-viewer-body"></div>' +
            '  <div class="pod-viewer-sources"></div>' +
            "</div>";
        document.body.appendChild(modal);
        function close() { modal.style.display = "none"; }
        modal.addEventListener("click", function (e) { if (e.target === modal) close(); });
        modal.querySelector(".pod-viewer-close").addEventListener("click", close);
        document.addEventListener("keydown", function (e) {
            if (e.key === "Escape" && modal.style.display !== "none") close();
        });
        return modal;
    }

    function _openPodViewer(podId) {
        var modal = _podModal();
        var titleEl = modal.querySelector(".pod-viewer-title");
        var metaEl = modal.querySelector(".pod-viewer-meta");
        var bodyEl = modal.querySelector(".pod-viewer-body");
        var srcEl = modal.querySelector(".pod-viewer-sources");
        titleEl.textContent = "Loading…";
        metaEl.textContent = "";
        bodyEl.innerHTML = "";
        srcEl.innerHTML = "";
        modal.style.display = "flex";
        fetch("/api/pods/" + encodeURIComponent(podId))
            .then(function (r) { if (!r.ok) throw new Error("unavailable"); return r.json(); })
            .then(function (data) {
                titleEl.textContent = data.one_liner || data.pod_id || podId;
                metaEl.textContent = data.pod_id || podId;
                var body = String(data.body || "");
                if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
                    bodyEl.innerHTML = DOMPurify.sanitize(marked.parse(body));
                } else {
                    bodyEl.textContent = body;
                }
                var urls = (data.source_urls || []).filter(function (u) {
                    return /^https?:\/\//i.test(String(u));  // only safe http(s) hrefs
                });
                if (urls.length) {
                    var label = document.createElement("div");
                    label.className = "pod-viewer-sources-label";
                    label.textContent = "Sources";
                    var ul = document.createElement("ul");
                    urls.forEach(function (u) {
                        var li = document.createElement("li");
                        var a = document.createElement("a");
                        a.href = String(u);
                        a.target = "_blank";
                        a.rel = "noopener";
                        a.textContent = String(u);
                        li.appendChild(a);
                        ul.appendChild(li);
                    });
                    srcEl.appendChild(label);
                    srcEl.appendChild(ul);
                }
            })
            .catch(function () {
                titleEl.textContent = "Finding not available";
                bodyEl.textContent = "This pod could not be loaded (it may be private or no longer stored).";
            });
    }

    function _renderMarkdown(text) {
        if (typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
            try {
                var raw = marked.parse(String(text || ""));
                var sanitized = DOMPurify.sanitize(raw);
                return _linkifyPods(_expandImagePods(sanitized));
            } catch (e) {
                // marked threw — render as plain text below
            }
        }
        // No marked? Still expand image pods + linkify on the raw text.
        return _linkifyPods(_expandImagePods(String(text || "")));
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
        _wirePodLinks(contentDiv);
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
