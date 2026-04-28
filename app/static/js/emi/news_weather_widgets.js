(function (global) {
    "use strict";

    function _getFns(ctx) {
        return {
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
            addOrUpdatePreference: function () {},
            removePreference: function () {},
        };
    }

    function addOrUpdatePreference(category, title, body, preference, id, ctx) {
        const fns = _getFns(ctx);
        _stateApi().addOrUpdatePreference({
            category: category,
            title: title,
            body: body,
            preference: preference,
            id: id,
        });
        fns.logDebug("Preference upserted for id:", id);
    }

    function removePreference(id, ctx) {
        const fns = _getFns(ctx);
        _stateApi().removePreference(id);
        fns.logDebug("Preference removed for id:", id);
    }

    function handleThumbs(action, event, ctx) {
        const fns = _getFns(ctx);
        const button = event.target.closest("button") || event.target;
        const parent = button.closest(".news-actions");
        if (!parent) {
            fns.logError("Could not find .news-actions parent for thumb click.");
            return;
        }

        const siblingButton = parent.querySelector(action === "up" ? ".thumbs-down" : ".thumbs-up");
        const newsId = button.dataset.id;

        const newsItemContainer = button.closest(".news-item");
        if (!newsItemContainer) {
            fns.logError("Could not find parent .news-item for vote.");
            return;
        }

        const titleElement = newsItemContainer.querySelector("h3 a");
        const summaryElement = newsItemContainer.querySelector("p");
        const title = titleElement ? titleElement.textContent.trim() : "Untitled";
        const body = summaryElement ? summaryElement.textContent.trim() : "No Summary";

        if (button.classList.contains("active")) {
            button.classList.remove("active");
            removePreference(newsId, ctx);
        } else {
            button.classList.add("active");
            if (siblingButton) {
                siblingButton.classList.remove("active");
                const siblingId = siblingButton.dataset.id;
                if (siblingId) {
                    removePreference(siblingId, ctx);
                }
            }
            addOrUpdatePreference("news", title, body, action === "up" ? "like" : "dislike", newsId, ctx);
        }
    }

    function updateWeatherWidget(weatherData, ctx) {
        const fns = _getFns(ctx);
        if (!Array.isArray(weatherData) || weatherData.length === 0 || !weatherData[0].data) {
            fns.logError("Invalid weather data format:", weatherData);
            return;
        }
        const dataObject = weatherData[0].data;
        if (!dataObject.weather_data || !Array.isArray(dataObject.weather_data) || dataObject.weather_data.length === 0) {
            fns.logError("Missing or empty weather_data:", dataObject);
            return;
        }
        const weather = dataObject.weather_data[0];
        const weatherWidget = document.querySelector(".weather-box");
        if (!weatherWidget) {
            fns.logError("Weather widget not found in DOM.");
            return;
        }
        if (!weather.location || !weather.location.name || !weather.location.country) {
            fns.logError("Location data is missing or malformed:", weather.location);
            return;
        }
        if (!weather.weather) {
            fns.logError("Weather conditions data is missing:", weather);
            return;
        }

        const location = weather.location.name + ", " + weather.location.country;
        const temperature = String((weather.weather.temperature || 0).toFixed(0));
        const condition = weather.weather.description || "";
        const high = "High: " + (weather.weather.max_temperature || "--");
        const low = "Low: " + (weather.weather.min_temperature || "--");

        global.globalSunrise = new Date(weather.sun_times?.sunrise || "").toLocaleTimeString();
        global.globalSunset = new Date(weather.sun_times?.sunset || "").toLocaleTimeString();

        const setEl = (id, text) => { const el = document.getElementById(id); if (el) el.textContent = text; };
        setEl("weather_temp_number", temperature);
        setEl("weather_place", location);
        setEl("weather_condition", condition);
        setEl("weather_high", high);
        setEl("weather_low", low);
    }

    function renderNewsWidget(newsData, _container, ctx) {
        const fns = _getFns(ctx);
        const validNewsItems = (Array.isArray(newsData) ? newsData : [])
            .filter(function (item) { return item && item.data_type && String(item.data_type).toLowerCase() === "news"; })
            .map(function (item) { return item.data; });

        const newsContainer = document.getElementById("news-widget");
        if (!newsContainer) {
            fns.logError("News container not found.");
            return;
        }

        if (!validNewsItems.length) {
            if (!newsContainer.children.length) {
                newsContainer.innerHTML = "<p>No news available.</p>";
            }
            return;
        }

        validNewsItems.forEach(function (news) {
            if (!news.link) {
                fns.logWarn("Skipping news item without a link:", news);
                return;
            }

            const existingItem = Array.from(newsContainer.children).find(function (child) {
                return child.dataset.newsId === news.link;
            });
            if (existingItem) return;

            const newsItem = document.createElement("div");
            newsItem.className = "news-item";
            newsItem.dataset.newsId = news.link;
            newsItem.innerHTML = `
            <h3><a href="${news.link}" target="_blank">${news.title || "No Title"}</a></h3>
            ${news.summary ? `<p>${news.summary}</p>` : ""}
            ${news.published ? `<p class="news-date">${new Date(news.published).toLocaleString("en-US", {
                month: "short",
                day: "numeric",
                year: "numeric",
                hour: "numeric",
                minute: "2-digit",
                hour12: true
            })}</p>` : ""}
            ${news.source ? `<p class="news-source">Source: ${news.source}</p>` : ""}
            <div class="news-footer">
              <div class="news-actions">
                <button class="thumbs-up" data-id="${news.link}" aria-label="Thumbs up">
                  <i class="fas fa-thumbs-up"></i>
                </button>
                <button class="thumbs-down" data-id="${news.link}" aria-label="Thumbs down">
                  <i class="fas fa-thumbs-down"></i>
                </button>
              </div>
            </div>
          `;

            newsContainer.prepend(newsItem);
            const thumbsUpBtn = newsItem.querySelector(".thumbs-up");
            const thumbsDownBtn = newsItem.querySelector(".thumbs-down");
            if (thumbsUpBtn) thumbsUpBtn.addEventListener("click", function (event) { handleThumbs("up", event, ctx); });
            if (thumbsDownBtn) thumbsDownBtn.addEventListener("click", function (event) { handleThumbs("down", event, ctx); });
        });
    }

    global.EmiNewsWeatherWidgets = {
        addOrUpdatePreference: addOrUpdatePreference,
        removePreference: removePreference,
        handleThumbs: handleThumbs,
        updateWeatherWidget: updateWeatherWidget,
        renderNewsWidget: renderNewsWidget,
    };
})(window);
