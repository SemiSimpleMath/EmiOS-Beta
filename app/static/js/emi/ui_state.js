(function (global) {
    "use strict";

    const _state = {
        todo: {
            byId: {},
            order: [],
        },
        email: {
            byId: {},
            order: [],
        },
        preferences: [],
    };

    function _asTaskId(value) {
        if (value === null || value === undefined) return "";
        const text = String(value).trim();
        return text;
    }

    function clearTodoTasks() {
        _state.todo.byId = {};
        _state.todo.order = [];
    }

    function upsertTodoTask(task) {
        if (!task || typeof task !== "object") return;
        const id = _asTaskId(task.id);
        if (!id) return;
        const existing = _state.todo.byId[id] || {};
        _state.todo.byId[id] = Object.assign({}, existing, task, { id: id });
        if (!_state.todo.order.includes(id)) {
            _state.todo.order.push(id);
        }
    }

    function setTodoTasks(tasks) {
        clearTodoTasks();
        if (!Array.isArray(tasks)) return;
        tasks.forEach(function (task) {
            upsertTodoTask(task);
        });
    }

    function getTodoTask(id) {
        const key = _asTaskId(id);
        if (!key) return null;
        return _state.todo.byId[key] || null;
    }

    function updateTodoCompletion(id, completed) {
        const key = _asTaskId(id);
        if (!key) return;
        const existing = _state.todo.byId[key];
        if (!existing) return;
        _state.todo.byId[key] = Object.assign({}, existing, {
            status: completed ? "completed" : "pending",
            completed: !!completed,
        });
    }

    function clearEmailItems() {
        _state.email.byId = {};
        _state.email.order = [];
    }

    function upsertEmailItem(email) {
        if (!email || typeof email !== "object") return;
        const id = _asTaskId(email.uid);
        if (!id) return;
        const existing = _state.email.byId[id] || {};
        _state.email.byId[id] = Object.assign({}, existing, email, { uid: id });
        if (!_state.email.order.includes(id)) {
            _state.email.order.push(id);
        }
    }

    function setEmailItems(items) {
        clearEmailItems();
        if (!Array.isArray(items)) return;
        items.forEach(function (email) {
            upsertEmailItem(email);
        });
    }

    function getEmailItem(uid) {
        const key = _asTaskId(uid);
        if (!key) return null;
        return _state.email.byId[key] || null;
    }

    function getPreferences() {
        return _state.preferences.slice();
    }

    function clearPreferences() {
        _state.preferences = [];
    }

    function addOrUpdatePreference(entry) {
        if (!entry || typeof entry !== "object") return;
        const id = _asTaskId(entry.id);
        if (!id) return;
        const idx = _state.preferences.findIndex(function (pref) {
            return _asTaskId(pref.id) === id;
        });
        const normalized = Object.assign({}, entry, { id: id });
        if (idx >= 0) {
            _state.preferences[idx] = normalized;
        } else {
            _state.preferences.push(normalized);
        }
    }

    function removePreference(id) {
        const key = _asTaskId(id);
        if (!key) return;
        _state.preferences = _state.preferences.filter(function (pref) {
            return _asTaskId(pref.id) !== key;
        });
    }

    global.EmiUiState = {
        clearTodoTasks: clearTodoTasks,
        upsertTodoTask: upsertTodoTask,
        setTodoTasks: setTodoTasks,
        getTodoTask: getTodoTask,
        updateTodoCompletion: updateTodoCompletion,
        clearEmailItems: clearEmailItems,
        upsertEmailItem: upsertEmailItem,
        setEmailItems: setEmailItems,
        getEmailItem: getEmailItem,
        getPreferences: getPreferences,
        clearPreferences: clearPreferences,
        addOrUpdatePreference: addOrUpdatePreference,
        removePreference: removePreference,
    };
})(window);
