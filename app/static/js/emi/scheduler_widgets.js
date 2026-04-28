(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getCurrentSchedulerDate: typeof ctx?.getCurrentSchedulerDate === "function" ? ctx.getCurrentSchedulerDate : function () { return new Date(); },
            setCurrentSchedulerDate: typeof ctx?.setCurrentSchedulerDate === "function" ? ctx.setCurrentSchedulerDate : function () {},
            getSchedulerEventsByDate: typeof ctx?.getSchedulerEventsByDate === "function" ? ctx.getSchedulerEventsByDate : function () { return {}; },
            getFormattedDate: typeof ctx?.getFormattedDate === "function" ? ctx.getFormattedDate : function (d) { return d.toISOString().slice(0, 10); },
            formatKeyForDisplay: typeof ctx?.formatKeyForDisplay === "function" ? ctx.formatKeyForDisplay : function (k) { return k; },
            formatValue: typeof ctx?.formatValue === "function" ? ctx.formatValue : function (_k, v) { return v; },
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    function addSchedulerItem(schedulerData, ctx) {
        const fns = _getFns(ctx);
        const data = schedulerData && schedulerData.data ? schedulerData.data : schedulerData;
        if (!data || typeof data !== "object") return;

        const eventTitle = data.title || "No Title";
        let eventDate;
        if (data.event_type === "interval" && data.occurrence) {
            eventDate = new Date(data.occurrence);
        } else if (data.start_date) {
            eventDate = new Date(data.start_date);
        } else {
            fns.logError("Scheduler event missing 'start_date' or 'occurrence':", data);
            return;
        }

        const dateKey = fns.getFormattedDate(new Date(eventDate.getTime()));
        const byDate = fns.getSchedulerEventsByDate();
        if (!byDate[dateKey]) byDate[dateKey] = [];

        const eventId = data.event_id;
        if (!eventId) {
            fns.logError("Scheduler event missing unique 'event_id':", data);
            return;
        }

        const existingEvent = byDate[dateKey].find(function (event) {
            return event.event_id === eventId;
        });
        if (existingEvent) {
            fns.logDebug("Event with ID " + eventId + " already exists for date " + dateKey + ". Skipping duplicate.");
            return;
        }

        byDate[dateKey].push(data);
        fns.logDebug('Adding scheduler event "' + eventTitle + '" to dateKey "' + dateKey + '"');

        if (dateKey === fns.getFormattedDate(fns.getCurrentSchedulerDate())) {
            renderSchedulerEvents(ctx);
        }
    }

    function handleSchedulerWidget(schedulerDataList, ctx) {
        const fns = _getFns(ctx);
        const schedulerList = document.getElementById("scheduler-list");
        if (!schedulerList) {
            fns.logError("Scheduler list element with ID 'scheduler-list' not found.");
            return;
        }

        schedulerList.innerHTML = "";
        const byDate = fns.getSchedulerEventsByDate();
        Object.keys(byDate).forEach(function (key) { delete byDate[key]; });

        if (!Array.isArray(schedulerDataList) || schedulerDataList.length === 0) {
            fns.logWarn("No scheduler data provided to handleSchedulerWidget.");
            schedulerList.innerHTML = "<li>No scheduled reminders.</li>";
            return;
        }

        schedulerDataList.forEach(function (schedulerData) {
            addSchedulerItem(schedulerData, ctx);
        });

        fns.logDebug("Scheduler refreshed with " + schedulerDataList.length + " event(s).");
        renderSchedulerEvents(ctx);
    }

    function renderSchedulerEvents(ctx) {
        const fns = _getFns(ctx);
        const schedulerList = document.getElementById("scheduler-list");
        if (!schedulerList) {
            fns.logError("Scheduler list element not found.");
            return;
        }

        const currentSchedulerDate = fns.getCurrentSchedulerDate();
        const currentDateKey = fns.getFormattedDate(currentSchedulerDate);
        const byDate = fns.getSchedulerEventsByDate();
        const events = byDate[currentDateKey] || [];

        const currentDateDisplay = document.getElementById("scheduler-current-date");
        if (currentDateDisplay) {
            currentDateDisplay.textContent = currentSchedulerDate.toDateString();
        } else {
            fns.logError("Element with ID 'scheduler-current-date' not found.");
        }

        schedulerList.innerHTML = "";
        if (events.length === 0) {
            schedulerList.innerHTML = "<li>No reminders for this day.</li>";
            return;
        }

        events.sort(function (a, b) {
            const timeA = a.occurrence ? new Date(a.occurrence).getTime() : (a.start_date ? new Date(a.start_date).getTime() : Infinity);
            const timeB = b.occurrence ? new Date(b.occurrence).getTime() : (b.start_date ? new Date(b.start_date).getTime() : Infinity);
            return timeA - timeB;
        });

        events.forEach(function (event) {
            const template = document.getElementById("scheduler-item-template");
            if (!template) {
                fns.logError("Scheduler item template with ID 'scheduler-item-template' not found.");
                return;
            }

            const schedulerItem = template.content.cloneNode(true);
            const minimalTitle = schedulerItem.querySelector(".scheduler__task-title");
            if (minimalTitle) minimalTitle.textContent = event.title || "No Title";

            const minimalDeadline = schedulerItem.querySelector(".scheduler__task-deadline");
            if (minimalDeadline) {
                let eventTime;
                if (event.occurrence) {
                    eventTime = new Date(event.occurrence).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
                } else if (event.start_date) {
                    eventTime = new Date(event.start_date).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
                } else {
                    fns.logError("No valid start time found for event:", event);
                    eventTime = "No Time";
                }
                minimalDeadline.textContent = eventTime;
            }

            const detailsView = schedulerItem.querySelector(".scheduler__item--details");
            if (detailsView) {
                detailsView.classList.add("hidden");
                Object.entries(event).forEach(function (entry) {
                    const key = entry[0];
                    const value = entry[1];
                    if (!value || key === "event_id" || key === "occurrence") return;
                    const detailLine = document.createElement("p");
                    detailLine.innerHTML =
                        "<strong>" + fns.formatKeyForDisplay(key) + ":</strong> " + fns.formatValue(key, value);
                    detailsView.appendChild(detailLine);
                });
            }

            const minimalView = schedulerItem.querySelector(".scheduler__item--minimal");
            if (minimalView && detailsView) {
                minimalView.addEventListener("click", function () {
                    detailsView.classList.toggle("hidden");
                });
            }

            schedulerList.appendChild(schedulerItem);
        });
    }

    function setupSchedulerNavigation(ctx) {
        const fns = _getFns(ctx);
        const prevBtn = document.getElementById("scheduler-prev-btn");
        const nextBtn = document.getElementById("scheduler-next-btn");

        if (!prevBtn || !nextBtn) {
            fns.logError("Scheduler navigation buttons not found.");
            return;
        }

        prevBtn.classList.add("arrow-btn");
        nextBtn.classList.add("arrow-btn");

        prevBtn.addEventListener("click", function () {
            const d = new Date(fns.getCurrentSchedulerDate());
            d.setDate(d.getDate() - 1);
            fns.setCurrentSchedulerDate(d);
            renderSchedulerEvents(ctx);
        });

        nextBtn.addEventListener("click", function () {
            const d = new Date(fns.getCurrentSchedulerDate());
            d.setDate(d.getDate() + 1);
            fns.setCurrentSchedulerDate(d);
            renderSchedulerEvents(ctx);
        });
    }

    global.EmiSchedulerWidgets = {
        handleSchedulerWidget: handleSchedulerWidget,
        addSchedulerItem: addSchedulerItem,
        renderSchedulerEvents: renderSchedulerEvents,
        setupSchedulerNavigation: setupSchedulerNavigation,
    };
})(window);
