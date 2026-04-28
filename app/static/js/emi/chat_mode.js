(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getChatMode: typeof ctx?.getChatMode === "function" ? ctx.getChatMode : function () { return "normal"; },
            setChatMode: typeof ctx?.setChatMode === "function" ? ctx.setChatMode : function () {},
        };
    }

    function updateModeIndicator(ctx) {
        const fns = _getFns(ctx);
        const indicatorId = ctx?.modeIndicatorId || "mode-indicator";
        const indicator = document.getElementById(indicatorId);
        if (!indicator) return;

        const chatMode = fns.getChatMode();
        switch (chatMode) {
            case "test":
                indicator.textContent = "TEST MODE";
                indicator.className = "mode-indicator test-mode";
                break;
            case "memo":
                indicator.textContent = "MEMO MODE";
                indicator.className = "mode-indicator memo-mode";
                break;
            default:
                indicator.textContent = "";
                indicator.className = "mode-indicator";
        }
    }

    function showModeNotification(message, ctx) {
        const notificationId = ctx?.notificationId || "mode-notification";
        const className = ctx?.notificationClassName || "mode-notification";
        let notification = document.getElementById(notificationId);
        if (!notification) {
            notification = document.createElement("div");
            notification.id = notificationId;
            notification.className = className;
            document.body.appendChild(notification);
        }

        notification.textContent = message;
        notification.style.display = "block";
        setTimeout(function () {
            notification.style.display = "none";
        }, 3000);
    }

    function toggleTestMode(ctx) {
        const fns = _getFns(ctx);
        const nextMode = fns.getChatMode() === "test" ? "normal" : "test";
        fns.setChatMode(nextMode);
        updateModeIndicator(ctx);
        showModeNotification("Test mode " + (nextMode === "test" ? "ON" : "OFF"), ctx);
        return nextMode;
    }

    function toggleMemoMode(ctx) {
        const fns = _getFns(ctx);
        const nextMode = fns.getChatMode() === "memo" ? "normal" : "memo";
        fns.setChatMode(nextMode);
        updateModeIndicator(ctx);
        showModeNotification("Memo mode " + (nextMode === "memo" ? "ON" : "OFF"), ctx);
        return nextMode;
    }

    global.EmiChatMode = {
        updateModeIndicator: updateModeIndicator,
        showModeNotification: showModeNotification,
        toggleTestMode: toggleTestMode,
        toggleMemoMode: toggleMemoMode,
    };
})(window);
