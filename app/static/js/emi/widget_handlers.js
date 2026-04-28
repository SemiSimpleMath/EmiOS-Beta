(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getHandlers: typeof ctx?.getHandlers === "function" ? ctx.getHandlers : function () { return {}; },
            handleGenericWidget: typeof ctx?.handleGenericWidget === "function" ? ctx.handleGenericWidget : function () {},
            normalizeWidgetGroup: typeof ctx?.normalizeWidgetGroup === "function" ? ctx.normalizeWidgetGroup : null,
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    function getWidgetHandler(widgetType, ctx) {
        const fns = _getFns(ctx);
        const handlers = fns.getHandlers() || {};
        if (!widgetType || typeof widgetType !== "string") {
            return fns.handleGenericWidget;
        }
        const key = widgetType.trim().toLowerCase();
        return handlers[key] || fns.handleGenericWidget;
    }

    function dispatchWidgetGroup(widgetType, widgetItems, ctx) {
        const fns = _getFns(ctx);
        let items = Array.isArray(widgetItems) ? widgetItems : [];
        if (typeof fns.normalizeWidgetGroup === "function") {
            items = fns.normalizeWidgetGroup(widgetType, items);
        } else if (global.EmiWidgetContract && typeof global.EmiWidgetContract.normalizeWidgetGroup === "function") {
            items = global.EmiWidgetContract.normalizeWidgetGroup(widgetType, items, { logWarn: fns.logWarn });
        }
        if (!Array.isArray(items) || items.length === 0) {
            fns.logDebug("No valid widget items after contract normalization for type: " + widgetType);
            return;
        }
        const handler = getWidgetHandler(widgetType, ctx);
        try {
            handler(items);
        } catch (widgetError) {
            fns.logError("Error processing widget type: " + widgetType, widgetError);
        }
    }

    function processRepoWidgetData(repoData, ctx) {
        const fns = _getFns(ctx);
        try {
            if (!repoData || !Array.isArray(repoData.widget_data) || repoData.widget_data.length === 0) {
                fns.logWarn("No repo widget data received or not in array format.");
                return;
            }

            const widgetsByType = {};
            repoData.widget_data.forEach(function (widgetItem) {
                if (!widgetItem.data_type || typeof widgetItem.data_type !== "string") {
                    fns.logWarn("Widget item missing or invalid 'data_type':", widgetItem);
                    fns.handleGenericWidget([widgetItem]);
                    return;
                }
                const widgetType = widgetItem.data_type.trim().toLowerCase();
                if (!widgetsByType[widgetType]) {
                    widgetsByType[widgetType] = [];
                }
                widgetsByType[widgetType].push(widgetItem);
            });

            Object.entries(widgetsByType).forEach(function (entry) {
                const widgetType = entry[0];
                const widgetItems = entry[1];
                fns.logDebug("Processing repo widget type: " + widgetType + " with " + widgetItems.length + " item(s)");
                dispatchWidgetGroup(widgetType, widgetItems, ctx);
            });
        } catch (error) {
            fns.logError("Unexpected error processing Repo Data:", error);
        }
    }

    global.EmiWidgetHandlers = {
        getWidgetHandler: getWidgetHandler,
        dispatchWidgetGroup: dispatchWidgetGroup,
        processRepoWidgetData: processRepoWidgetData,
    };
})(window);
