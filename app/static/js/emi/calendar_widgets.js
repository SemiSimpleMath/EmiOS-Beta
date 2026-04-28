(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getCurrentCalendarDate: typeof ctx?.getCurrentCalendarDate === "function" ? ctx.getCurrentCalendarDate : function () { return new Date(); },
            setCurrentCalendarDate: typeof ctx?.setCurrentCalendarDate === "function" ? ctx.setCurrentCalendarDate : function () {},
            getCalendarEventsByDate: typeof ctx?.getCalendarEventsByDate === "function" ? ctx.getCalendarEventsByDate : function () { return {}; },
            getFormattedDate: typeof ctx?.getFormattedDate === "function" ? ctx.getFormattedDate : function (d) { return d.toISOString().slice(0, 10); },
            formatKeyForDisplay: typeof ctx?.formatKeyForDisplay === "function" ? ctx.formatKeyForDisplay : function (k) { return k; },
            formatValue: typeof ctx?.formatValue === "function" ? ctx.formatValue : function (_k, v) { return v; },
            logDebug: typeof ctx?.logDebug === "function" ? ctx.logDebug : console.log,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    function initializeCurrentCalendarDate(ctx) {
        const fns = _getFns(ctx);
        const eventsByDate = fns.getCalendarEventsByDate();
        const dates = Object.keys(eventsByDate || {});
        if (dates.length > 0) {
            dates.sort();
            fns.setCurrentCalendarDate(new Date(dates[0]));
            return;
        }
        fns.setCurrentCalendarDate(new Date());
    }

    function setupCalendarNavigation(ctx) {
        const fns = _getFns(ctx);
        const prevBtn = document.getElementById("calendar-prev-btn");
        const nextBtn = document.getElementById("calendar-next-btn");

        if (!prevBtn || !nextBtn) {
            fns.logError("Calendar navigation buttons not found.");
            return;
        }

        prevBtn.addEventListener("click", function () {
            const d = new Date(fns.getCurrentCalendarDate());
            d.setDate(d.getDate() - 1);
            fns.setCurrentCalendarDate(d);
            renderCalendarEvents(ctx);
        });

        nextBtn.addEventListener("click", function () {
            const d = new Date(fns.getCurrentCalendarDate());
            d.setDate(d.getDate() + 1);
            fns.setCurrentCalendarDate(d);
            renderCalendarEvents(ctx);
        });
    }

    function renderCalendarEvents(ctx) {
        const fns = _getFns(ctx);
        const calendarList = document.getElementById("calendar-list");
        if (!calendarList) {
            fns.logError("Calendar list element with ID 'calendar-list' not found.");
            return;
        }

        const currentCalendarDate = fns.getCurrentCalendarDate();
        const currentDateKey = fns.getFormattedDate(currentCalendarDate);
        const eventsByDate = fns.getCalendarEventsByDate() || {};
        const events = Array.isArray(eventsByDate[currentDateKey]) ? eventsByDate[currentDateKey] : [];

        fns.logDebug('Rendering events for dateKey "' + currentDateKey + '" with ' + events.length + " event(s).");

        const currentDateDisplay = document.getElementById("calendar-current-date");
        if (currentDateDisplay) {
            currentDateDisplay.textContent = currentCalendarDate.toDateString();
        } else {
            fns.logError("Element with ID 'calendar-current-date' not found.");
        }

        calendarList.innerHTML = "";
        if (events.length === 0) {
            calendarList.innerHTML = "<li>No events for this day.</li>";
            return;
        }

        events.sort(function (a, b) {
            const aAllDay = a.is_all_day === true;
            const bAllDay = b.is_all_day === true;
            if (aAllDay && !bAllDay) return -1;
            if (!aAllDay && bAllDay) return 1;

            function getStartTime(event) {
                if (event.start && typeof event.start === "string") return new Date(event.start).getTime();
                if (event.start_time) return new Date(event.start_time).getTime();
                if (event.start && typeof event.start === "object" && event.start.dateTime) {
                    return new Date(event.start.dateTime).getTime();
                }
                if (event.start_dateTime) return new Date(event.start_dateTime).getTime();
                return 0;
            }
            return getStartTime(a) - getStartTime(b);
        });

        events.forEach(function (event) {
            const template = document.getElementById("calendar-item-template");
            if (!template) {
                fns.logError("Calendar item template with ID 'calendar-item-template' not found.");
                return;
            }

            const calendarItem = template.content.cloneNode(true);
            const minimalTitle = calendarItem.querySelector(".calendar__event-title");
            const minimalTime = calendarItem.querySelector(".calendar__event-time");
            const timeOptions = { hour: "numeric", minute: "2-digit" };

            if (event.is_all_day === true) {
                if (minimalTime) minimalTime.textContent = "All Day";
            } else {
                let eventStartTime = "No Start Time";
                if (event.start && typeof event.start === "string") {
                    eventStartTime = new Date(event.start).toLocaleTimeString([], timeOptions);
                }
                let eventEndTime = null;
                if (event.end && typeof event.end === "string") {
                    eventEndTime = new Date(event.end).toLocaleTimeString([], timeOptions);
                }
                if (minimalTime) {
                    if (eventEndTime) minimalTime.innerHTML = eventStartTime + "<br>" + eventEndTime;
                    else minimalTime.textContent = eventStartTime;
                }
            }

            var status = (event.status || "").toLowerCase();
            if (minimalTitle) {
                var title = event.summary || "No Title";
                if (status === "cancelled") {
                    minimalTitle.innerHTML = "<s>" + title + "</s>";
                    minimalTitle.style.opacity = "0.5";
                } else if (status === "completed") {
                    minimalTitle.innerHTML = "\u2713 " + title;
                    minimalTitle.style.opacity = "0.6";
                } else {
                    minimalTitle.textContent = title;
                }
            } else {
                fns.logError(".calendar__event-title not found in template.");
            }

            const detailsView = calendarItem.querySelector(".calendar__item--details");
            if (detailsView) {
                detailsView.classList.add("hidden");
                const excludedKeys = ["data_type"];
                Object.entries(event).forEach(function (entry) {
                    const key = entry[0];
                    const value = entry[1];
                    if (excludedKeys.includes(key) || !value || value === "N/A") return;
                    const detailLine = document.createElement("p");
                    detailLine.innerHTML =
                        "<strong>" + fns.formatKeyForDisplay(key) + ":</strong> " + fns.formatValue(key, value);
                    detailsView.appendChild(detailLine);
                });
            } else {
                fns.logError(".calendar__item--details not found in template.");
            }

            const minimalView = calendarItem.querySelector(".calendar__item--minimal");
            if (minimalView && detailsView) {
                minimalView.addEventListener("click", function () {
                    detailsView.classList.toggle("hidden");
                });
            } else {
                fns.logError("Minimal view or details view elements not found in calendar item.");
            }

            calendarList.appendChild(calendarItem);
        });
    }

    global.EmiCalendarWidgets = {
        initializeCurrentCalendarDate: initializeCurrentCalendarDate,
        setupCalendarNavigation: setupCalendarNavigation,
        renderCalendarEvents: renderCalendarEvents,
    };
})(window);
