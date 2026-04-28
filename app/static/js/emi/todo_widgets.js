(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            callToolRoute: typeof ctx?.callToolRoute === "function" ? ctx.callToolRoute : async function () { return null; },
            addFeedItem: typeof ctx?.addFeedItem === "function" ? ctx.addFeedItem : function () {},
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    function _stateApi() {
        if (global.EmiUiState && typeof global.EmiUiState === "object") {
            return global.EmiUiState;
        }
        return {
            clearTodoTasks: function () {},
            upsertTodoTask: function () {},
            updateTodoCompletion: function () {},
        };
    }

    function _extractTaskData(taskData) {
        if (taskData && typeof taskData === "object" && taskData.data && typeof taskData.data === "object") {
            return taskData.data;
        }
        return taskData;
    }

    function handleTodoWidget(todoDataList, ctx) {
        const fns = _getFns(ctx);
        const todoListContainer = document.getElementById("todo-list");
        if (!todoListContainer) {
            fns.logError("To-Do list container with ID 'todo-list' not found.");
            return;
        }

        const list = Array.isArray(todoDataList) ? todoDataList.slice() : [];
        list.sort(function (a, b) {
            const aData = _extractTaskData(a) || {};
            const bData = _extractTaskData(b) || {};
            const aDue = new Date(aData.due || 0).getTime();
            const bDue = new Date(bData.due || 0).getTime();
            return aDue - bDue;
        });

        _stateApi().clearTodoTasks();
        todoListContainer.innerHTML = "";
        fns.logDebug("To-Do list cleared for full refresh.");

        if (list.length === 0) {
            fns.logWarn("No To-Do data provided to handleTodoWidget.");
            todoListContainer.innerHTML = "<li class='todo-empty'>No tasks available.</li>";
            return;
        }

        list.forEach(function (todoData) {
            addTodoItem(todoData, ctx);
        });

        fns.logDebug("To-Do list refreshed with " + list.length + " task(s).");
    }

    function addTodoItem(taskData, ctx) {
        const fns = _getFns(ctx);
        const task = _extractTaskData(taskData);
        const todoListContainer = document.getElementById("todo-list");
        const todoTemplate = document.getElementById("todo-item-template");

        if (!todoListContainer || !todoTemplate) {
            fns.logError("Todo list container or template not found.");
            return;
        }
        if (!task || typeof task !== "object") {
            fns.logError("Task data invalid:", taskData);
            return;
        }

        const taskId = task.id;
        if (!taskId) {
            fns.logError("Task data missing unique 'id':", task);
            return;
        }

        const taskIdText = String(taskId).trim();
        const existingTask = Array.from(todoListContainer.children).some(function (item) {
            return item.dataset.taskId === taskIdText;
        });
        if (existingTask) {
            fns.logDebug("Task with ID " + taskIdText + " already exists. Skipping duplicate.");
            return;
        }

        const taskElement = todoTemplate.content.cloneNode(true);
        const listItem = taskElement.querySelector(".todo-item");
        const checkbox = listItem.querySelector("input[type='checkbox']");
        const taskText = listItem.querySelector(".todo-task");
        const dueText = listItem.querySelector(".todo-due");
        const notesText = listItem.querySelector(".todo-notes");

        checkbox.id = "task-" + taskIdText;
        listItem.querySelector("label").setAttribute("for", checkbox.id);

        taskText.textContent = task.title || "No Title";
        listItem.dataset.taskId = taskIdText;

        const isCompleted = task.status === "completed";
        if (isCompleted) {
            checkbox.checked = true;
            taskText.classList.add("completed");
            listItem.classList.add("completed-task");
        }

        if (task.due) {
            dueText.textContent = "Due: " + new Date(task.due).toLocaleDateString();
        }
        if (task.notes) {
            notesText.textContent = task.notes;
        }

        if (isCompleted) {
            todoListContainer.appendChild(listItem);
        } else {
            todoListContainer.prepend(listItem);
        }

        _stateApi().upsertTodoTask(task);
        fns.logDebug("Added task with ID " + taskIdText + ": \"" + (task.title || "No Title") + "\"");

        attachTodoCheckboxHandlers(ctx);
    }

    function attachTodoCheckboxHandlers(ctx) {
        const fns = _getFns(ctx);
        document.querySelectorAll(".todo-item input[type='checkbox']").forEach(function (checkbox) {
            if (checkbox.dataset.handlerAttached === "true") return;

            checkbox.addEventListener("change", async function () {
                const listItem = checkbox.closest(".todo-item");
                const taskId = listItem.dataset.taskId;
                const taskText = listItem.querySelector(".todo-task");
                const isChecked = checkbox.checked;
                const taskTitle = taskText && taskText.textContent ? taskText.textContent.trim() : "Untitled";

                taskText.classList.toggle("completed", isChecked);
                listItem.classList.toggle("completed-task", isChecked);
                _stateApi().updateTodoCompletion(taskId, isChecked);

                const payload = {
                    data_type: "ui_tool_request",
                    tool_name: "update_todo_task",
                    action: "update todo task",
                    meta_data: {
                        title: taskTitle,
                        marked_as: isChecked ? "done" : "needs to be done",
                    },
                    arguments: {
                        task_id: taskId,
                        completed: !!isChecked,
                    },
                };

                try {
                    const response = await fns.callToolRoute("ToDoTool", "update_todo_task", payload);
                    if (response && response.agent_response) {
                        fns.addFeedItem(response.agent_response);
                    }
                } catch (err) {
                    fns.logError("Error calling tool route:", err);
                }
            });

            checkbox.dataset.handlerAttached = "true";
        });
    }

    global.EmiTodoWidgets = {
        handleTodoWidget: handleTodoWidget,
        addTodoItem: addTodoItem,
        attachTodoCheckboxHandlers: attachTodoCheckboxHandlers,
    };
})(window);
