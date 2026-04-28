(function (global) {
    "use strict";

    function _nonEmptyString(value) {
        return typeof value === "string" && value.trim() !== "";
    }

    function _groupWidgets(widgetDataList, deps) {
        const grouped = {};
        for (const widgetItem of widgetDataList) {
            if (!widgetItem || typeof widgetItem !== "object") {
                deps.logWarn("Widget item is not an object:", widgetItem);
                continue;
            }
            if (!_nonEmptyString(widgetItem.data_type)) {
                deps.logWarn("Widget item missing or invalid 'data_type':", widgetItem);
                if (typeof deps.handleGenericWidget === "function") {
                    deps.handleGenericWidget([widgetItem]);
                }
                continue;
            }
            const widgetType = widgetItem.data_type.trim().toLowerCase();
            if (!Array.isArray(grouped[widgetType])) {
                grouped[widgetType] = [];
            }
            grouped[widgetType].push(widgetItem);
        }
        return grouped;
    }

    function _handleUserMessageData(data, deps) {
        deps.logDebug("Received UserMessageData:", data);

        if (_nonEmptyString(data && data.chat) && typeof deps.renderChat === "function") {
            var chatMeta = null;
            if (Array.isArray(data.sub_data_type) && data.sub_data_type.length > 0) {
                chatMeta = { sub_data_type: data.sub_data_type };
            }
            deps.renderChat(data.chat, chatMeta);
        }

        if (_nonEmptyString(data && data.feed) && typeof deps.renderFeed === "function") {
            deps.renderFeed(data.feed);
        }

        if (_nonEmptyString(data && data.sound) && typeof deps.renderSound === "function") {
            deps.renderSound(data.sound.trim());
        }

        if (Array.isArray(data && data.widget_data) && data.widget_data.length > 0) {
            const grouped = _groupWidgets(data.widget_data, deps);
            for (const [widgetType, widgetItems] of Object.entries(grouped)) {
                try {
                    deps.renderWidgetGroup(widgetType, widgetItems);
                } catch (e) {
                    deps.logError("Error rendering widget group:", widgetType, e);
                }
            }
            return;
        }
        deps.logDebug("No widget data in this message.");
    }

    function wireSocketIngress(options) {
        const opts = options || {};
        const socket = opts.socket;
        if (!socket || typeof socket.on !== "function" || typeof socket.emit !== "function") {
            throw new Error("wireSocketIngress requires a valid Socket.IO socket.");
        }

        const deps = {
            renderChat: opts.renderChat,
            renderFeed: opts.renderFeed,
            renderSound: opts.renderSound,
            renderWidgetGroup: opts.renderWidgetGroup,
            handleGenericWidget: opts.handleGenericWidget,
            onAudioFile: opts.onAudioFile,
            onRepoUpdateNotification: opts.onRepoUpdateNotification,
            onConnect: opts.onConnect,
            onDisconnect: opts.onDisconnect,
            logDebug: typeof opts.logDebug === "function" ? opts.logDebug : console.log,
            logWarn: typeof opts.logWarn === "function" ? opts.logWarn : console.warn,
            logError: typeof opts.logError === "function" ? opts.logError : console.error,
        };

        socket.on("connect", () => {
            try {
                if (typeof deps.onConnect === "function") {
                    deps.onConnect();
                }
            } catch (e) {
                deps.logError("Socket connect handler failed:", e);
            }
        });

        socket.on("disconnect", () => {
            try {
                if (typeof deps.onDisconnect === "function") {
                    deps.onDisconnect();
                }
            } catch (e) {
                deps.logError("Socket disconnect handler failed:", e);
            }
        });

        socket.on("user_message_data", (data) => {
            try {
                _handleUserMessageData(data, deps);
            } catch (e) {
                deps.logError("Unexpected error processing UserMessageData:", e);
            }
        });

        socket.on("audio_file", (audioData) => {
            try {
                if (typeof deps.onAudioFile === "function") {
                    deps.onAudioFile(audioData);
                }
            } catch (e) {
                deps.logError("Error processing audio_file event:", e);
            }
        });

        socket.on("repo_update_notification", (updateData) => {
            try {
                if (typeof deps.onRepoUpdateNotification === "function") {
                    deps.onRepoUpdateNotification(updateData);
                }
            } catch (e) {
                deps.logError("Error processing repo_update_notification:", e);
            }
        });
    }

    global.EmiSocketIngress = {
        wireSocketIngress: wireSocketIngress,
    };
})(window);
