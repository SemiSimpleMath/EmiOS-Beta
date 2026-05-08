(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
            getSpeakingMode: typeof ctx?.getSpeakingMode === "function" ? ctx.getSpeakingMode : function () { return false; },
            setSpeakingMode: typeof ctx?.setSpeakingMode === "function" ? ctx.setSpeakingMode : function () {},
            getAudioPlayer: typeof ctx?.getAudioPlayer === "function" ? ctx.getAudioPlayer : function () { return null; },
            showModeNotification: typeof ctx?.showModeNotification === "function" ? ctx.showModeNotification : function () {},
            showNotification: typeof ctx?.showNotification === "function" ? ctx.showNotification : function () {},
            fetchImpl: typeof ctx?.fetchImpl === "function" ? ctx.fetchImpl : fetch,
            logWarn: typeof ctx?.logWarn === "function" ? ctx.logWarn : console.warn,
            logError: typeof ctx?.logError === "function" ? ctx.logError : console.error,
        };
    }

    function toggleTheme() {
        const html = document.documentElement;
        const currentTheme = html.getAttribute("data-theme");
        const newTheme = currentTheme === "light" ? "dark" : "light";
        html.setAttribute("data-theme", newTheme);
    }

    function updateThemeIcon() {
        const html = document.documentElement;
        const currentTheme = html.getAttribute("data-theme");
        const menuThemeBtn = document.getElementById("menu-theme-toggle");
        if (!menuThemeBtn) return;
        const icon = menuThemeBtn.querySelector("i");
        if (icon) {
            icon.className = currentTheme === "light" ? "fas fa-moon" : "fas fa-sun";
        }
    }

    function setupThemeListener(ctx) {
        const menuThemeBtn = document.getElementById("menu-theme-toggle");
        if (menuThemeBtn) {
            menuThemeBtn.addEventListener("click", function () {
                toggleTheme();
                updateThemeIcon();
            });
        }
    }

    function setupMenuToggle(ctx) {
        const fns = _getFns(ctx);
        const menuToggleBtn = document.getElementById("menu-toggle-btn");
        const dropdownMenu = document.getElementById("dropdown-menu");
        const devToggleBtn = document.getElementById("dev-menu-toggle-btn");
        const devDropdownMenu = document.getElementById("dev-dropdown-menu");
        const lifeToggleBtn = document.getElementById("life-menu-toggle-btn");
        const lifeDropdownMenu = document.getElementById("life-dropdown-menu");

        if (!menuToggleBtn || !dropdownMenu) {
            fns.logWarn("Menu elements not found");
            return;
        }

        menuToggleBtn.addEventListener("click", function (e) {
            e.stopPropagation();
            dropdownMenu.classList.toggle("hidden");
            if (devDropdownMenu) devDropdownMenu.classList.add("hidden");
            if (lifeDropdownMenu) lifeDropdownMenu.classList.add("hidden");
        });

        if (devToggleBtn && devDropdownMenu) {
            devToggleBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                devDropdownMenu.classList.toggle("hidden");
                dropdownMenu.classList.add("hidden");
                if (lifeDropdownMenu) lifeDropdownMenu.classList.add("hidden");
            });
        }

        if (lifeToggleBtn && lifeDropdownMenu) {
            lifeToggleBtn.addEventListener("click", function (e) {
                e.stopPropagation();
                lifeDropdownMenu.classList.toggle("hidden");
                dropdownMenu.classList.add("hidden");
                if (devDropdownMenu) devDropdownMenu.classList.add("hidden");
            });
        }

        document.addEventListener("click", function (e) {
            if (!dropdownMenu.contains(e.target) && e.target !== menuToggleBtn) {
                dropdownMenu.classList.add("hidden");
            }
            if (devDropdownMenu && !devDropdownMenu.contains(e.target) && e.target !== devToggleBtn) {
                devDropdownMenu.classList.add("hidden");
            }
            if (lifeDropdownMenu && !lifeDropdownMenu.contains(e.target) && e.target !== lifeToggleBtn) {
                lifeDropdownMenu.classList.add("hidden");
            }
        });

        const submenuToggles = document.querySelectorAll(".submenu-toggle");
        submenuToggles.forEach(function (toggle) {
            toggle.addEventListener("click", function (e) {
                e.stopPropagation();
                const parentItem = toggle.closest(".menu-submenu");
                if (parentItem) {
                    parentItem.classList.toggle("open");
                }
            });
        });

        const menuSpeakBtn = document.getElementById("menu-speak-mode");
        if (menuSpeakBtn) {
            menuSpeakBtn.addEventListener("click", function () {
                fns.setSpeakingMode(!fns.getSpeakingMode());

                const audioPlayer = fns.getAudioPlayer();
                if (/iP(hone|ad|od)/.test(navigator.userAgent) && audioPlayer) {
                    const SILENT = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=";
                    audioPlayer.src = SILENT;
                    audioPlayer.play()
                        .then(function () {
                            audioPlayer.pause();
                            audioPlayer.src = "";
                        })
                        .catch(function (e) {
                            fns.logWarn("Audio unlock failed", e);
                        });
                }
            });
        }

        const menuQuietBtn = document.getElementById("menu-quiet-mode");
        if (menuQuietBtn) {
            function updateQuietModeUI(isEnabled) {
                const span = menuQuietBtn.querySelector("span");
                const icon = menuQuietBtn.querySelector("i");
                if (span) {
                    span.textContent = isEnabled ? "Quiet Mode: On" : "Quiet Mode: Off";
                }
                if (icon) {
                    icon.className = isEnabled ? "fas fa-moon" : "fas fa-moon";
                }
                menuQuietBtn.setAttribute("aria-pressed", isEnabled);
                menuQuietBtn.classList.toggle("active", isEnabled);
            }

            fns.fetchImpl("/api/settings/quiet-mode")
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data.success) {
                        updateQuietModeUI(data.quiet_mode.enabled);
                    }
                })
                .catch(function (err) { fns.logError("Error loading quiet mode state:", err); });

            menuQuietBtn.addEventListener("click", async function () {
                try {
                    const response = await fns.fetchImpl("/api/settings/quiet-mode/toggle", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                    });
                    const data = await response.json();

                    if (data.success) {
                        updateQuietModeUI(data.enabled);
                        fns.showNotification(data.message, "success");
                    } else {
                        fns.showNotification(data.message || "Failed to toggle quiet mode", "error");
                    }
                } catch (error) {
                    fns.logError("Error toggling quiet mode:", error);
                    fns.showNotification("Error toggling quiet mode", "error");
                }
            });
        }

        const screenCaptureBtn = document.getElementById("menu-screen-capture-toggle");
        if (screenCaptureBtn) {
            function updateScreenCaptureUI(isEnabled) {
                const span = screenCaptureBtn.querySelector("span");
                if (span) {
                    span.textContent = isEnabled ? "Screen Capture: Armed" : "Screen Capture: Disarmed";
                }
                screenCaptureBtn.setAttribute("aria-pressed", isEnabled ? "true" : "false");
                screenCaptureBtn.setAttribute("data-active", isEnabled ? "true" : "false");
            }

            fns.fetchImpl("/api/dev/screen-capture-control")
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data && data.success) {
                        updateScreenCaptureUI(!!data.enabled);
                    }
                })
                .catch(function (err) { fns.logError("Error loading screen capture toggle state:", err); });

            screenCaptureBtn.addEventListener("click", async function (e) {
                e.preventDefault();
                e.stopPropagation();
                const isActive = screenCaptureBtn.getAttribute("data-active") === "true";
                const desired = !isActive;
                try {
                    const response = await fns.fetchImpl("/api/dev/screen-capture-control", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ enabled: desired }),
                    });
                    const data = await response.json();
                    if (response.ok && data && data.success) {
                        updateScreenCaptureUI(!!data.enabled);
                        fns.showModeNotification(data.enabled ? "Screen capture armed" : "Screen capture disarmed");
                    } else {
                        fns.showModeNotification((data && data.message) || "Failed to update screen capture toggle");
                    }
                } catch (error) {
                    fns.logError("Error toggling screen capture:", error);
                    fns.showModeNotification("Error toggling screen capture");
                }
            });
        }

        const verboseLogBtn = document.getElementById("menu-verbose-log-toggle");
        if (verboseLogBtn) {
            function updateVerboseUI(isEnabled) {
                const span = verboseLogBtn.querySelector("span");
                if (span) {
                    span.textContent = isEnabled ? "Verbose Logs: On" : "Verbose Logs: Off";
                }
                verboseLogBtn.setAttribute("aria-pressed", isEnabled ? "true" : "false");
                verboseLogBtn.setAttribute("data-active", isEnabled ? "true" : "false");
            }

            fns.fetchImpl("/debug/logging/verbose")
                .then(function (res) { return res.json(); })
                .then(function (data) {
                    if (data && data.success) {
                        updateVerboseUI(!!data.verbose);
                    }
                })
                .catch(function (err) { fns.logError("Error loading verbose log state:", err); });

            verboseLogBtn.addEventListener("click", async function (e) {
                e.preventDefault();
                e.stopPropagation();
                var isActive = verboseLogBtn.getAttribute("data-active") === "true";
                try {
                    var response = await fns.fetchImpl("/debug/logging/verbose", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ enabled: !isActive }),
                    });
                    var data = await response.json();
                    if (response.ok && data && data.success) {
                        updateVerboseUI(!!data.verbose);
                        fns.showModeNotification(data.verbose ? "All logs → terminal" : "LLM logs only → terminal");
                    }
                } catch (error) {
                    fns.logError("Error toggling verbose logs:", error);
                    fns.showModeNotification("Error toggling verbose logs");
                }
            });
        }

        const shutdownBtn = document.getElementById("menu-shutdown");
        if (shutdownBtn) {
            shutdownBtn.addEventListener("click", async function () {
                if (!confirm("Shut down EmiOS? You will need to restart from the terminal.")) return;
                try {
                    await fns.fetchImpl("/api/shutdown", { method: "POST" });
                    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#888;"><h2>EmiOS has been shut down. Close this tab.</h2></div>';
                } catch (error) {
                    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#888;"><h2>EmiOS has been shut down. Close this tab.</h2></div>';
                }
            });
        }

    }

    global.EmiMenuControls = {
        setupThemeListener: setupThemeListener,
        toggleTheme: toggleTheme,
        updateThemeIcon: updateThemeIcon,
        setupMenuToggle: setupMenuToggle,
    };
})(window);
