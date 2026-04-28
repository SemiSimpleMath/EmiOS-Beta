(function (global) {
    "use strict";

    const _warnedKeys = new Set();

    function _warnOnce(logWarn, key, message, payload) {
        if (_warnedKeys.has(key)) return;
        _warnedKeys.add(key);
        logWarn(message, payload);
    }

    function _asObject(value) {
        return value && typeof value === "object" ? value : null;
    }

    function _ensureEnvelope(item) {
        const obj = _asObject(item);
        if (!obj) return null;
        const hasData = _asObject(obj.data);
        if (hasData) return obj;
        const copy = Object.assign({}, obj);
        copy.data = Object.assign({}, obj);
        return copy;
    }

    function _normalizeEmail(item) {
        const wrapped = _ensureEnvelope(item);
        if (!wrapped) return null;
        const data = wrapped.data;
        if (!data.uid && typeof data.id === "string" && data.id.trim()) {
            data.uid = data.id.trim();
        }
        if (!data.uid && typeof data.message_id === "string" && data.message_id.trim()) {
            data.uid = data.message_id.trim();
        }
        if (!data.uid) return null;
        return wrapped;
    }

    function _normalizeCalendar(item) {
        const wrapped = _ensureEnvelope(item);
        if (!wrapped) return null;
        const data = wrapped.data;
        if (!data.id && typeof data.event_id === "string" && data.event_id.trim()) {
            data.id = data.event_id.trim();
        }
        if (!data.summary && typeof data.event_name === "string" && data.event_name.trim()) {
            data.summary = data.event_name.trim();
        }
        if (!data.start && typeof data.start_date === "string" && data.start_date.trim()) {
            data.start = data.start_date.trim();
        }
        if (!data.id || !data.start) return null;
        return wrapped;
    }

    function _normalizeScheduler(item) {
        const wrapped = _ensureEnvelope(item);
        if (!wrapped) return null;
        const data = wrapped.data;
        if (!data.event_id && typeof data.id === "string" && data.id.trim()) {
            data.event_id = data.id.trim();
        }
        if (!data.start_date && typeof data.start === "string" && data.start.trim()) {
            data.start_date = data.start.trim();
        }
        const hasTime = !!(data.occurrence || data.start_date);
        if (!data.event_id || !hasTime) return null;
        return wrapped;
    }

    function normalizeWidgetGroup(widgetType, widgetItems, opts) {
        const options = opts || {};
        const logWarn = typeof options.logWarn === "function" ? options.logWarn : console.warn;
        const type = typeof widgetType === "string" ? widgetType.trim().toLowerCase() : "";
        if (!Array.isArray(widgetItems)) return [];

        let normalizer = null;
        if (type === "email") normalizer = _normalizeEmail;
        else if (type === "calendar") normalizer = _normalizeCalendar;
        else if (type === "scheduler") normalizer = _normalizeScheduler;
        else return widgetItems;

        const out = [];
        for (const item of widgetItems) {
            const normalized = normalizer(item);
            if (normalized) {
                out.push(normalized);
                continue;
            }
            _warnOnce(
                logWarn,
                "contract_drop_" + type,
                "[widget_contract] Dropping invalid '" + type + "' widget item.",
                item
            );
        }
        return out;
    }

    global.EmiWidgetContract = {
        normalizeWidgetGroup: normalizeWidgetGroup,
    };
})(window);
