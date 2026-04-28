(function (global) {
    "use strict";

    function _getFns(ctx) {
        const fallback = function () {};
        return {
            getPendingImageFile: typeof ctx?.getPendingImageFile === "function" ? ctx.getPendingImageFile : function () { return null; },
            setPendingImageFile: typeof ctx?.setPendingImageFile === "function" ? ctx.setPendingImageFile : fallback,
            getPendingImageObjectUrl: typeof ctx?.getPendingImageObjectUrl === "function" ? ctx.getPendingImageObjectUrl : function () { return null; },
            setPendingImageObjectUrl: typeof ctx?.setPendingImageObjectUrl === "function" ? ctx.setPendingImageObjectUrl : fallback,
            getAllowedImageMimes: typeof ctx?.getAllowedImageMimes === "function" ? ctx.getAllowedImageMimes : function () { return new Set(); },
            getMaxImageBytes: typeof ctx?.getMaxImageBytes === "function" ? ctx.getMaxImageBytes : function () { return 0; },
            alertUser: typeof ctx?.alertUser === "function" ? ctx.alertUser : function (message) { alert(message); },
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
        };
    }

    function updatePendingImagePreviewUI(ctx) {
        const fns = _getFns(ctx);
        const containerId = ctx?.previewContainerId || "pending-image-preview";
        const thumbId = ctx?.previewThumbId || "pending-image-thumb";
        const nameId = ctx?.previewNameId || "pending-image-name";
        const container = document.getElementById(containerId);
        const img = document.getElementById(thumbId);
        const nameEl = document.getElementById(nameId);
        if (!container || !img || !nameEl) return;

        const pendingImageFile = fns.getPendingImageFile();
        const pendingImageObjectUrl = fns.getPendingImageObjectUrl();
        if (!pendingImageFile || !pendingImageObjectUrl) {
            container.classList.add("hidden");
            img.removeAttribute("src");
            nameEl.textContent = "";
            return;
        }

        img.src = pendingImageObjectUrl;
        nameEl.textContent = pendingImageFile.name || "image";
        container.classList.remove("hidden");
    }

    function clearPendingImage(ctx) {
        const fns = _getFns(ctx);
        const objectUrl = fns.getPendingImageObjectUrl();
        try {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
        } catch (e) {
            fns.logWarn("Failed to revoke pending image object URL", e);
        }
        fns.setPendingImageObjectUrl(null);
        fns.setPendingImageFile(null);
        updatePendingImagePreviewUI(ctx);
    }

    function setPendingImage(file, ctx) {
        if (!file) return;
        const fns = _getFns(ctx);
        const mime = file.type || "";
        const allowedImageMimes = fns.getAllowedImageMimes();
        if (!allowedImageMimes.has(mime)) {
            fns.alertUser("Only PNG, JPEG, WEBP, or GIF images are supported.");
            return;
        }
        const maxImageBytes = fns.getMaxImageBytes();
        if (file.size > maxImageBytes) {
            fns.alertUser("Image is too large (max 20MB).");
            return;
        }

        clearPendingImage(ctx);
        fns.setPendingImageFile(file);
        fns.setPendingImageObjectUrl(URL.createObjectURL(file));
        updatePendingImagePreviewUI(ctx);
    }

    function getPendingImageFile(ctx) {
        const fns = _getFns(ctx);
        return fns.getPendingImageFile();
    }

    global.EmiAttachments = {
        updatePendingImagePreviewUI: updatePendingImagePreviewUI,
        clearPendingImage: clearPendingImage,
        setPendingImage: setPendingImage,
        getPendingImageFile: getPendingImageFile,
    };
})(window);
