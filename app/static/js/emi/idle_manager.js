(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getIdleTimeout: typeof ctx?.getIdleTimeout === "function" ? ctx.getIdleTimeout : function () { return null; },
            setIdleTimeout: typeof ctx?.setIdleTimeout === "function" ? ctx.setIdleTimeout : function () {},
            getIdleInterval: typeof ctx?.getIdleInterval === "function" ? ctx.getIdleInterval : function () { return null; },
            setIdleInterval: typeof ctx?.setIdleInterval === "function" ? ctx.setIdleInterval : function () {},
            getIdleThresholdMs: typeof ctx?.getIdleThresholdMs === "function" ? ctx.getIdleThresholdMs : function () { return 60000; },
            getIdleCheckIntervalMs: typeof ctx?.getIdleCheckIntervalMs === "function" ? ctx.getIdleCheckIntervalMs : function () { return 60000; },
            getSocketId: typeof ctx?.getSocketId === "function" ? ctx.getSocketId : function () { return ""; },
            getPreferenceList: typeof ctx?.getPreferenceList === "function" ? ctx.getPreferenceList : function () { return []; },
            clearPreferenceList: typeof ctx?.clearPreferenceList === "function" ? ctx.clearPreferenceList : function () {},
            clearAudioChunks: typeof ctx?.clearAudioChunks === "function" ? ctx.clearAudioChunks : function () {},
            fetchImpl: typeof ctx?.fetchImpl === "function" ? ctx.fetchImpl : fetch,
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    function invokeIdleRoute(ctx) {
        const fns = _getFns(ctx);
        const socketId = fns.getSocketId();
        const preferenceList = fns.getPreferenceList();

        fns.logDebug("Invoking idle route");
        fns.fetchImpl("/handle_idle_route", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                socket_id: socketId,
                timestamp: Date.now(),
                preferences: preferenceList,
            }),
        })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                if (data && data.success) {
                    fns.logDebug("Idle route invoked successfully:", data.message);
                    fns.clearPreferenceList();
                    return;
                }
                fns.logError("Error invoking idle route:", data && data.message ? data.message : "unknown error");
            })
            .catch(function (error) {
                fns.logError("Failed to invoke idle route:", error);
            });

        fns.clearAudioChunks();
    }

    function resetIdleTimer(ctx) {
        const fns = _getFns(ctx);
        const idleTimeout = fns.getIdleTimeout();
        if (idleTimeout) {
            clearTimeout(idleTimeout);
            fns.setIdleTimeout(null);
        }

        const idleInterval = fns.getIdleInterval();
        if (idleInterval) {
            clearInterval(idleInterval);
            fns.setIdleInterval(null);
        }

        const idleThreshold = fns.getIdleThresholdMs();
        const idleCheckInterval = fns.getIdleCheckIntervalMs();
        const timeoutId = setTimeout(function () {
            fns.logDebug("Idle timeout reached. Invoking idle route...");
            invokeIdleRoute(ctx);

            const intervalId = setInterval(function () {
                fns.logDebug("Recurring idle interval reached. Invoking idle route again...");
                invokeIdleRoute(ctx);
            }, idleCheckInterval);
            fns.setIdleInterval(intervalId);
            fns.logDebug("Set up idleInterval with check every " + (idleCheckInterval / 60000) + " minutes.");
        }, idleThreshold);

        fns.setIdleTimeout(timeoutId);
    }

    function attachIdleListeners(ctx) {
        const fns = _getFns(ctx);
        const activityEvents = ["mousemove", "mousedown", "keypress", "keydown", "wheel", "touchstart"];
        activityEvents.forEach(function (eventType) {
            document.addEventListener(eventType, function () {
                resetIdleTimer(ctx);
            });
        });
        fns.logDebug("Idle listeners attached. Timer will reset on user activity.");
    }

    function setupVisibilityIdleHooks(ctx) {
        const fns = _getFns(ctx);
        document.addEventListener("visibilitychange", function () {
            if (document.hidden) {
                try {
                    invokeIdleRoute(ctx);
                } catch (e) {
                    fns.logWarn("visibilitychange idle ping failed", e);
                }
            }
        });
        window.addEventListener("pagehide", function () {
            try {
                invokeIdleRoute(ctx);
            } catch (e) {
                fns.logWarn("pagehide idle ping failed", e);
            }
        });
    }

    global.EmiIdleManager = {
        invokeIdleRoute: invokeIdleRoute,
        resetIdleTimer: resetIdleTimer,
        attachIdleListeners: attachIdleListeners,
        setupVisibilityIdleHooks: setupVisibilityIdleHooks,
    };
})(window);
