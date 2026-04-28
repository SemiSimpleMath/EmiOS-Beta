/**
 * socket_heartbeat.js — shared liveness + replay helper for Emi's socket clients.
 *
 * What it does:
 *   1. Tracks `lastMessageTs` — the ISO timestamp of the newest message the
 *      client has rendered. Pages should call window.EmiSocketHeartbeat.
 *      noteIncoming(data) when they render a user_message_data payload.
 *   2. Every `heartbeatIntervalMs` it emits a `heartbeat` event. If no
 *      `heartbeat_ack` arrives within `heartbeatTimeoutMs`, the socket is
 *      considered dead (classic Cloudflare-tunnel zombie) and we force a
 *      reconnect via socket.disconnect(); socket.connect().
 *   3. On reconnect, the page's onConnect re-emits `register_chat_client`
 *      with `since_ts = lastMessageTs`. The server replays any assistant
 *      messages missed during the outage.
 *
 * Usage:
 *   window.EmiSocketHeartbeat.install({
 *     socket,
 *     getRoomId: () => "master_room",
 *     registerEventName: "register_chat_client",
 *   });
 *
 * The page must also call `window.EmiSocketHeartbeat.noteIncoming(data)` from
 * its user_message_data listener so `lastMessageTs` stays current.
 */
(function () {
    if (window.EmiSocketHeartbeat) return;  // idempotent load guard

    const HEARTBEAT_INTERVAL_MS = 10000;  // 10s — rate of liveness probes
    const HEARTBEAT_TIMEOUT_MS = 3000;    // 3s — wait for ack before declaring dead

    let lastMessageTs = null;       // ISO string of newest message the UI has rendered

    function _toIso(raw) {
        if (!raw) return null;
        try {
            if (typeof raw === "number") return new Date(raw).toISOString();
            // Accept Date, ISO string, etc.
            const d = new Date(raw);
            if (Number.isNaN(d.getTime())) return null;
            return d.toISOString();
        } catch (e) {
            return null;
        }
    }

    function noteIncoming(data) {
        if (!data) return;
        // The emitted payload doesn't always carry a timestamp directly; the
        // convention in this codebase is that "now" is close enough for replay
        // cutoff purposes (unified_log stores the real server ts).
        lastMessageTs = new Date().toISOString();
    }

    function getLastMessageTs() {
        return lastMessageTs;
    }

    function install(opts) {
        const socket = opts && opts.socket;
        const getRoomId = opts && opts.getRoomId;
        if (!socket || typeof getRoomId !== "function") {
            console.error("[EmiSocketHeartbeat] install: socket and getRoomId are required");
            return;
        }
        const intervalMs = opts.heartbeatIntervalMs || HEARTBEAT_INTERVAL_MS;
        const timeoutMs = opts.heartbeatTimeoutMs || HEARTBEAT_TIMEOUT_MS;

        let pending = false;         // heartbeat in flight waiting for ack
        let pendingTimer = null;     // ack-timeout timer
        let intervalHandle = null;
        let reconnecting = false;    // avoid re-triggering during a forced cycle

        function cancelPending() {
            pending = false;
            if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; }
        }

        function forceReconnect(reason) {
            if (reconnecting) return;
            reconnecting = true;
            console.warn("[EmiSocketHeartbeat] force reconnect —", reason);
            cancelPending();
            // Pause the heartbeat loop during the reconnect gap. Without this,
            // a new heartbeat can fire in the 250ms window between disconnect
            // and connect, or before register_chat_client lands — creating a
            // brief "alive but unbound" zombie on the server.
            if (intervalHandle) { clearInterval(intervalHandle); intervalHandle = null; }
            try { socket.disconnect(); } catch (e) { /* ignore */ }
            setTimeout(() => {
                try { socket.connect(); } catch (e) { console.error("[EmiSocketHeartbeat] reconnect error:", e); }
                // Resume heartbeat probes. The socket.on("connect") handler in
                // the page code will have re-emitted register_chat_client by
                // the time this fires.
                intervalHandle = setInterval(sendHeartbeat, intervalMs);
                reconnecting = false;
            }, 250);
        }

        socket.on("heartbeat_ack", () => { cancelPending(); });

        // Server tells us our room binding was taken over by another tab.
        // Stop auto-reconnecting and surface a banner so the user isn't
        // silently wondering why messages stop arriving. They can refresh
        // (which re-registers and hijacks the new tab back) or close this
        // tab if they want to keep using the newer one.
        socket.on("socket_hijacked", (info) => {
            try {
                console.warn("[EmiSocketHeartbeat] socket_hijacked:", info);
                if (typeof socket.io === "object" && typeof socket.io.reconnection === "function") {
                    socket.io.reconnection(false);
                }
                cancelPending();
                if (intervalHandle) { clearInterval(intervalHandle); intervalHandle = null; }

                let banner = document.getElementById("emi-socket-hijack-banner");
                if (!banner) {
                    banner = document.createElement("div");
                    banner.id = "emi-socket-hijack-banner";
                    banner.style.cssText = (
                        "position:fixed;top:0;left:0;right:0;z-index:10000;" +
                        "background:#a33;color:#fff;padding:10px 14px;" +
                        "font:500 13px/1.4 system-ui,sans-serif;text-align:center;"
                    );
                    banner.innerHTML = (
                        "This tab's connection was taken over by a newer tab. " +
                        "<a href=\"javascript:location.reload()\" style=\"color:#fff;text-decoration:underline\">Reload</a> " +
                        "to use this tab again, or close it."
                    );
                    document.body.appendChild(banner);
                }
            } catch (e) {
                console.error("[EmiSocketHeartbeat] socket_hijacked handler error:", e);
            }
        });

        // Every `intervalMs`, send a heartbeat. If no ack within `timeoutMs`,
        // assume the socket is a zombie and force reconnect.
        function sendHeartbeat() {
            if (!socket.connected) return;  // will retry on next tick
            if (pending) return;            // one in flight already
            pending = true;
            try {
                socket.emit("heartbeat", {});
            } catch (e) {
                console.warn("[EmiSocketHeartbeat] heartbeat emit failed:", e);
                pending = false;
                return;
            }
            pendingTimer = setTimeout(() => {
                if (!pending) return;
                forceReconnect("no heartbeat_ack within " + timeoutMs + "ms");
            }, timeoutMs);
        }

        if (intervalHandle) clearInterval(intervalHandle);
        intervalHandle = setInterval(sendHeartbeat, intervalMs);

        // Cancel any pending heartbeat on explicit disconnect — a fresh cycle
        // will start after the next connect.
        socket.on("disconnect", () => { cancelPending(); });
    }

    window.EmiSocketHeartbeat = {
        install,
        noteIncoming,
        getLastMessageTs,
    };
})();
