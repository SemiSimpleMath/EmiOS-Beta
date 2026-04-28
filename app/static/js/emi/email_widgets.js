(function (global) {
    "use strict";

    let emailDateSlotMissingLogged = false;

    function _getFns(ctx) {
        return {
            formatKeyForDisplay: typeof ctx?.formatKeyForDisplay === "function" ? ctx.formatKeyForDisplay : function (k) { return k; },
            formatValue: typeof ctx?.formatValue === "function" ? ctx.formatValue : function (_k, v) { return v; },
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
            clearEmailItems: function () {},
            upsertEmailItem: function () {},
        };
    }

    function _extractEmailData(emailData) {
        if (emailData && typeof emailData === "object" && emailData.data && typeof emailData.data === "object") {
            return emailData.data;
        }
        return emailData;
    }

    function handleEmailWidget(emailDataList, ctx) {
        const fns = _getFns(ctx);
        const emailList = document.getElementById("email-list");
        const emailTemplate = document.getElementById("email-item-template");

        if (!emailList || !emailTemplate) {
            fns.logError("Email list container or template not found.");
            return;
        }

        _stateApi().clearEmailItems();
        emailList.innerHTML = "";
        fns.logDebug("Email list cleared for full refresh.");

        const list = Array.isArray(emailDataList) ? emailDataList : [];
        if (list.length === 0) {
            fns.logWarn("No email data provided to handleEmailWidget.");
            emailList.innerHTML = "<li>No emails.</li>";
            return;
        }

        list.forEach(function (emailData) {
            addEmailItem(emailData, ctx);
        });

        fns.logDebug("Email list refreshed with " + list.length + " email(s).");
    }

    function addEmailItem(emailData, ctx) {
        const fns = _getFns(ctx);
        const data = _extractEmailData(emailData);
        const emailList = document.getElementById("email-list");
        if (!emailList) {
            fns.logError("Email list element with ID 'email-list' not found.");
            return;
        }
        if (!data || typeof data !== "object") {
            fns.logError("Email data invalid:", emailData);
            return;
        }

        const emailUid = data.uid;
        if (!emailUid) {
            fns.logError("Email data missing unique 'uid':", data);
            return;
        }
        const uidText = String(emailUid).trim();

        const existingItems = Array.from(emailList.children);
        const alreadyExists = existingItems.some(function (item) {
            return item.dataset.emailUid === uidText;
        });
        if (alreadyExists) {
            fns.logDebug("Email with UID " + uidText + " already exists. Skipping.");
            return;
        }

        const template = document.getElementById("email-item-template");
        if (!template) {
            fns.logError("Email item template with ID 'email-item-template' not found.");
            return;
        }

        const emailItem = template.content.cloneNode(true);
        const emailElement = emailItem.querySelector(".email__item");
        if (emailElement) {
            emailElement.dataset.emailUid = uidText;
        } else {
            fns.logError(".email__item not found in template.");
        }

        const minimalSender = emailItem.querySelector(".email__sender");
        const minimalSubject = emailItem.querySelector(".email__subject");
        const minimalDate = emailItem.querySelector(".email__date");

        if (minimalSender) minimalSender.textContent = data.sender || "Unknown Sender";
        else fns.logError(".email__sender not found in template.");

        if (minimalSubject) minimalSubject.textContent = data.subject || "No Subject";
        else fns.logError(".email__subject not found in template.");

        if (minimalDate) {
            minimalDate.textContent = data.date ? new Date(data.date).toLocaleString() : "";
        } else if (!emailDateSlotMissingLogged) {
            fns.logDebug("Email template has no .email__date slot; skipping minimal date render.");
            emailDateSlotMissingLogged = true;
        }

        const detailsView = emailItem.querySelector(".email__item--details");
        if (detailsView) {
            detailsView.classList.add("hidden");
            Object.entries(data).forEach(function (entry) {
                const key = entry[0];
                const value = entry[1];
                if (!value || key === "uid") return;
                const detailLine = document.createElement("p");
                detailLine.innerHTML =
                    "<strong>" + fns.formatKeyForDisplay(key) + ":</strong> " + fns.formatValue(key, value);
                detailsView.appendChild(detailLine);
            });
        } else {
            fns.logError(".email__item--details not found in template.");
        }

        const minimalView = emailItem.querySelector(".email__item--minimal");
        if (minimalView && detailsView) {
            minimalView.addEventListener("click", function () {
                detailsView.classList.toggle("hidden");
            });
        } else {
            fns.logError("Minimal view or details view elements not found in email item.");
        }

        emailList.prepend(emailItem);
        _stateApi().upsertEmailItem(data);
    }

    global.EmiEmailWidgets = {
        handleEmailWidget: handleEmailWidget,
        addEmailItem: addEmailItem,
    };
})(window);
