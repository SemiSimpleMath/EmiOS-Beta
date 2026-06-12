// Timeline renderer — vanilla SVG, no framework.
//
// Three zoom tiers:
//   'overview'      — full range. If span > 15 yr, group into 5-year bins;
//                     else step year-by-year. Per-bin display budget.
//   'bin-detail'    — one 5-year bin. Per-year budget inside it. Only used
//                     when overview is binned.
//   'year-detail'   — one year, X spread by month. Per-month budget;
//                     "+N more" toggles a month to show everything.
//
// A breadcrumb back button steps one level up. Importance drives dot size +
// opacity; category drives color; date confidence drives stroke style.
// Prose-only entries live in their own "Undated" lane.

(function () {
    "use strict";

    const SVG_NS = "http://www.w3.org/2000/svg";

    const CATEGORY_COLORS = {
        marriage: "#f76b6b",
        parenthood: "#f76b6b",
        "parent-child relationship": "#f76b6b",
        family: "#f76b6b",
        residence: "#7c9af7",
        occupation: "#9ad07c",
        employment: "#9ad07c",
        career: "#9ad07c",
        education: "#d7a8ff",
        health: "#ffb37c",
        belief: "#7cc7d6",
        preference: "#aaaaaa",
        contact: "#ffd97c",
        ownership: "#c79aff",
        event: "#888888",
    };

    function colorForCategory(cat) {
        if (!cat) return "#888";
        const lower = String(cat).toLowerCase();
        for (const key of Object.keys(CATEGORY_COLORS)) {
            if (lower.includes(key)) return CATEGORY_COLORS[key];
        }
        return "#7c5af7";
    }

    function dotRadius(imp) {
        if (imp === null || imp === undefined) return 4;
        if (imp >= 9) return 10;
        if (imp >= 7) return 8;
        if (imp >= 5) return 6;
        if (imp >= 3) return 4;
        return 3;
    }
    function dotOpacity(imp) {
        if (imp === null || imp === undefined) return 0.5;
        if (imp >= 9) return 1.0;
        if (imp >= 7) return 0.9;
        if (imp >= 5) return 0.75;
        if (imp >= 3) return 0.6;
        return 0.35;
    }

    function strokeForConfidence(c) {
        if (!c || c === "actual") return null;
        if (c === "inferred") return "3,2";
        if (c === "estimated") return "2,3";
        return "1,3";
    }

    // Density-adaptive curation: per-bin display budgets by zoom tier.
    // The global filters (importance slider + confidence toggles) apply
    // first as a floor; if a bin still exceeds its budget only the top-K
    // survive (see curationOrder) and a "+N more" badge owns up to the rest.
    const BUDGET_OVERVIEW_PER_YEAR = 4;  // overview + bin-detail: per year (or per 5-yr bin) cluster
    const BUDGET_YEAR_PER_MONTH = 8;     // year-detail: per month; "+N more" expands the month in place
    // month-detail: unlimited — everything that passes the global filters.
    const BIN_SIZE = 5;               // years per bin
    const BIN_THRESHOLD = 15;          // span > this triggers binning in overview
    const UNDATED_LABEL_CAP = 5;       // max labeled prose-only entries in lane

    let entries = [];
    let entryYearMin = 1970;
    let entryYearMax = 2030;
    let totalEntries = 0;

    // Mode state
    let mode = "overview";        // 'overview' | 'bin-detail' | 'year-detail' | 'month-detail'
    let detailBinStart = null;    // bin-detail anchor
    let detailYear = null;        // year-detail anchor (also set in month-detail)
    let detailMonth = null;       // month-detail anchor (1..12)
    let pageIndex = 0;            // page within overview (binned or year-by-year)
    const expandedMonths = new Set(); // year-detail: month bins toggled past their budget; reset on zoom change

    function getContainerWidth() {
        const wrap = document.querySelector(".timeline-wrap");
        if (!wrap) return 1200;
        // .timeline-wrap has 56px padding L+R for the arrows; subtract both sides.
        return Math.max(700, wrap.clientWidth - 112);
    }

    async function loadData() {
        const res = await fetch(`/api/timeline/${encodeURIComponent(window.ENTITY_ID)}`);
        const data = await res.json();
        if (!data.ok) {
            console.error("timeline data load failed", data);
            return;
        }
        entries = data.entries || [];
        entryYearMin = data.min_year;
        entryYearMax = data.max_year;
        totalEntries = data.total || entries.length;
        document.getElementById("totalCount").textContent = totalEntries;
        buildCategoryLegend(entries);
        render();
    }

    function buildCategoryLegend(items) {
        const seen = new Set();
        const legend = document.querySelector(".category-legend");
        if (!legend) return;
        items.forEach((e) => { if (e.category) seen.add(e.category); });
        const cats = Array.from(seen).sort();
        legend.innerHTML = "";
        cats.slice(0, 14).forEach((cat) => {
            const span = document.createElement("span");
            span.className = "category-swatch";
            span.innerHTML = `<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:${colorForCategory(cat)};margin-right:2px"></span>${cat}`;
            legend.appendChild(span);
        });
    }

    function getFilters() {
        const minImp = parseFloat(document.getElementById("impFilter").value);
        document.getElementById("impFilterVal").textContent = minImp.toFixed(1);
        return {
            minImp,
            showEstimated: document.getElementById("showEstimated").checked,
            showProseOnly: document.getElementById("showProseOnly").checked,
        };
    }

    function entryPasses(e, filt) {
        const imp = e.importance;
        if (imp !== null && imp !== undefined && imp < filt.minImp) return false;
        const c = e.date_confidence;
        if (!filt.showEstimated && c && c !== "actual" && c !== "" && e.date_iso) return false;
        if (!filt.showProseOnly && !e.date_iso) return false;
        return true;
    }

    // Curation order within a bin: importance desc (null = -1), then date
    // confidence (hard dates beat inferred beat estimated beat the rest),
    // then date_iso asc. Array.sort is stable, so ties keep input order —
    // deterministic across renders.
    const CONFIDENCE_RANK = { actual: 0, user_set: 0, explicit: 0, inferred: 1, estimated: 2 };
    function confidenceRank(c) {
        const r = CONFIDENCE_RANK[c];
        return r === undefined ? 3 : r;
    }
    function curationOrder(a, b) {
        const ai = a.importance == null ? -1 : a.importance;
        const bi = b.importance == null ? -1 : b.importance;
        if (bi !== ai) return bi - ai;
        const ac = confidenceRank(a.date_confidence);
        const bc = confidenceRank(b.date_confidence);
        if (ac !== bc) return ac - bc;
        const ad = a.date_iso || "";
        const bd = b.date_iso || "";
        return ad < bd ? -1 : ad > bd ? 1 : 0;
    }

    function binFor(year) {
        return Math.floor(year / BIN_SIZE) * BIN_SIZE;
    }

    function setModeOverview() {
        mode = "overview";
        detailBinStart = null;
        detailYear = null;
        detailMonth = null;
        pageIndex = 0;
        expandedMonths.clear();
        render();
    }
    function setModeBinDetail(binStart) {
        mode = "bin-detail";
        detailBinStart = binStart;
        detailYear = null;
        detailMonth = null;
        pageIndex = 0;
        expandedMonths.clear();
        render();
    }
    function setModeYearDetail(year) {
        mode = "year-detail";
        detailYear = year;
        detailMonth = null;
        pageIndex = 0;
        expandedMonths.clear();
        render();
    }
    function setModeMonthDetail(year, month) {
        mode = "month-detail";
        detailYear = year;
        detailMonth = month;
        pageIndex = 0;
        expandedMonths.clear();
        render();
    }

    // Page state ─ set when each render computes total pages.
    let lastTotalPages = 1;

    function nextPage() {
        if (pageIndex < lastTotalPages - 1) {
            pageIndex += 1;
            render();
        }
    }
    function prevPage() {
        if (pageIndex > 0) {
            pageIndex -= 1;
            render();
        }
    }
    function updatePageControls(totalPages) {
        lastTotalPages = totalPages;
        const prev = document.getElementById("prevPageBtn");
        const next = document.getElementById("nextPageBtn");
        const indicator = document.getElementById("pageIndicator");
        if (!prev || !next || !indicator) return;
        if (totalPages <= 1) {
            prev.classList.add("hidden");
            next.classList.add("hidden");
            indicator.classList.add("hidden");
            return;
        }
        prev.classList.toggle("hidden", pageIndex === 0);
        next.classList.toggle("hidden", pageIndex === totalPages - 1);
        indicator.classList.remove("hidden");
        indicator.textContent = `${pageIndex + 1} / ${totalPages}`;
    }

    window.prevPage = prevPage;
    window.nextPage = nextPage;
    function stepBack() {
        if (mode === "month-detail") {
            setModeYearDetail(detailYear);
            return;
        }
        if (mode === "year-detail") {
            // If we came from a bin context (overview was binned), step to bin-detail.
            // Else go all the way back to overview.
            const dated = entries.filter((e) => e.year != null);
            const yearMin = dated.length ? Math.min(...dated.map((e) => e.year)) : entryYearMin;
            const yearMax = dated.length ? Math.max(...dated.map((e) => e.year)) : entryYearMax;
            const span = yearMax - yearMin;
            if (span > BIN_THRESHOLD) {
                setModeBinDetail(binFor(detailYear));
            } else {
                setModeOverview();
            }
        } else {
            setModeOverview();
        }
    }

    function updateModeButton() {
        const btn = document.getElementById("backToOverviewBtn");
        if (!btn) return;
        const label = btn.querySelector(".btn-year-label");
        if (mode === "month-detail") {
            btn.classList.remove("hidden");
            const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
            label.textContent = `${monthNames[detailMonth - 1]} ${detailYear}`;
        } else if (mode === "year-detail") {
            btn.classList.remove("hidden");
            label.textContent = `year ${detailYear}`;
        } else if (mode === "bin-detail") {
            btn.classList.remove("hidden");
            label.textContent = `${detailBinStart}–${detailBinStart + BIN_SIZE - 1}`;
        } else {
            btn.classList.add("hidden");
        }
    }

    function render() {
        updateModeButton();
        if (mode === "month-detail") {
            renderMonthDetail(detailYear, detailMonth);
        } else if (mode === "year-detail") {
            renderYearDetail(detailYear);
        } else if (mode === "bin-detail") {
            renderBinDetail(detailBinStart);
        } else {
            renderOverview();
        }
    }

    // ── Overview mode ──────────────────────────────────────────────────────
    function renderOverview() {
        const svg = document.getElementById("timelineSvg");
        svg.innerHTML = "";
        const filt = getFilters();
        const visible = entries.filter((e) => entryPasses(e, filt));
        document.getElementById("visibleCount").textContent = visible.length;

        if (visible.length === 0) {
            renderEmpty(svg, "No entries match current filters.");
            updatePageControls(1);
            return;
        }

        const PAD_LEFT = 60;
        const PAD_RIGHT = 40;
        const AXIS_Y = 40;
        const ROW_HEIGHT = 30;
        const ROW_TOP = AXIS_Y + 18;

        const dated = visible.filter((e) => e.year != null);
        const proseOnly = visible.filter((e) => e.year == null);
        const yearMin = dated.length ? Math.min(...dated.map((e) => e.year)) : entryYearMin;
        const yearMax = dated.length ? Math.max(...dated.map((e) => e.year)) : entryYearMax;
        const span = yearMax - yearMin;
        const useBins = span > BIN_THRESHOLD;
        const W = getContainerWidth();

        if (useBins) {
            const binStart = binFor(yearMin);
            const binEnd = binFor(yearMax) + BIN_SIZE - 1;
            const allBins = [];
            for (let b = binStart; b <= binEnd - BIN_SIZE + 1; b += BIN_SIZE) {
                allBins.push(b);
            }
            // ~110px per bin → fit as many as the container can hold.
            const binsPerPage = Math.max(2, Math.floor((W - PAD_LEFT - PAD_RIGHT) / 110));
            const totalPages = Math.max(1, Math.ceil(allBins.length / binsPerPage));
            pageIndex = Math.min(pageIndex, totalPages - 1);
            const pageStart = pageIndex * binsPerPage;
            const pageBins = allBins.slice(pageStart, pageStart + binsPerPage);
            const pageBinStart = pageBins[0];
            const pageBinEnd = pageBins[pageBins.length - 1] + BIN_SIZE - 1;

            const binToX = (start) => {
                const i = (start - pageBinStart) / BIN_SIZE;
                const denom = Math.max(1, pageBins.length - 1);
                return PAD_LEFT + (i / denom) * (W - PAD_LEFT - PAD_RIGHT);
            };

            const byBin = {};
            dated.forEach((e) => {
                const b = binFor(e.year);
                if (b >= pageBinStart && b <= pageBinEnd) {
                    (byBin[b] = byBin[b] || []).push(e);
                }
            });

            renderAxisBins(svg, pageBinStart, pageBinEnd, binToX, byBin);

            let maxRowsAny = 0;
            pageBins.forEach((b) => {
                const list = (byBin[b] || []).slice().sort(curationOrder);
                if (list.length === 0) return;
                const shown = list.slice(0, BUDGET_OVERVIEW_PER_YEAR);
                const hidden = list.length - shown.length;
                const x = binToX(b);
                shown.forEach((e, i) => {
                    maxRowsAny = Math.max(maxRowsAny, i + 1);
                    const yPx = ROW_TOP + i * ROW_HEIGHT;
                    drawEntry(svg, e, x, yPx, /*showLabel*/ true);
                });
                if (hidden > 0) {
                    const row = shown.length;
                    maxRowsAny = Math.max(maxRowsAny, row + 1);
                    const yPx = ROW_TOP + row * ROW_HEIGHT;
                    drawMoreBadge(svg, x, yPx, hidden, () => setModeBinDetail(b),
                        `Showing ${shown.length} of ${list.length} for ${b}–${b + BIN_SIZE - 1} — click to zoom in`);
                }
            });

            // Undated lane only on last page so it doesn't repeat.
            if (pageIndex === totalPages - 1) {
                maxRowsAny = renderUndatedLane(svg, proseOnly, W, PAD_LEFT, PAD_RIGHT, ROW_TOP, ROW_HEIGHT, maxRowsAny);
            }
            finalizeViewBox(svg, W, ROW_TOP, maxRowsAny, ROW_HEIGHT);
            updatePageControls(totalPages);
        } else {
            // Year-by-year overview
            const allYears = [];
            for (let y = yearMin; y <= yearMax; y++) allYears.push(y);
            const yearsPerPage = Math.max(3, Math.floor((W - PAD_LEFT - PAD_RIGHT) / 60));
            const totalPages = Math.max(1, Math.ceil(allYears.length / yearsPerPage));
            pageIndex = Math.min(pageIndex, totalPages - 1);
            const pageStart = pageIndex * yearsPerPage;
            const pageYears = allYears.slice(pageStart, pageStart + yearsPerPage);
            const pageYearMin = pageYears[0];
            const pageYearMax = pageYears[pageYears.length - 1];

            const yearToX = (year) => {
                const denom = Math.max(1, pageYearMax - pageYearMin);
                return PAD_LEFT + ((year - pageYearMin) / denom) * (W - PAD_LEFT - PAD_RIGHT);
            };
            const byYear = {};
            dated.forEach((e) => {
                if (e.year >= pageYearMin && e.year <= pageYearMax) {
                    (byYear[e.year] = byYear[e.year] || []).push(e);
                }
            });

            renderAxisYears(svg, pageYearMin, pageYearMax, yearToX, byYear, (y) => setModeYearDetail(y));

            let maxRowsAny = 0;
            pageYears.forEach((y) => {
                const list = (byYear[y] || []).slice().sort(curationOrder);
                if (list.length === 0) return;
                const shown = list.slice(0, BUDGET_OVERVIEW_PER_YEAR);
                const hidden = list.length - shown.length;
                const x = yearToX(y);
                shown.forEach((e, i) => {
                    maxRowsAny = Math.max(maxRowsAny, i + 1);
                    const yPx = ROW_TOP + i * ROW_HEIGHT;
                    drawEntry(svg, e, x, yPx, /*showLabel*/ true);
                });
                if (hidden > 0) {
                    const row = shown.length;
                    maxRowsAny = Math.max(maxRowsAny, row + 1);
                    const yPx = ROW_TOP + row * ROW_HEIGHT;
                    drawMoreBadge(svg, x, yPx, hidden, () => setModeYearDetail(y),
                        `Showing ${shown.length} of ${list.length} for ${y} — click to zoom in`);
                }
            });

            if (pageIndex === totalPages - 1) {
                maxRowsAny = renderUndatedLane(svg, proseOnly, W, PAD_LEFT, PAD_RIGHT, ROW_TOP, ROW_HEIGHT, maxRowsAny);
            }
            finalizeViewBox(svg, W, ROW_TOP, maxRowsAny, ROW_HEIGHT);
            updatePageControls(totalPages);
        }
    }

    // ── Bin-detail mode (one 5-year window, top-N per year) ───────────────
    function renderBinDetail(binStart) {
        const svg = document.getElementById("timelineSvg");
        svg.innerHTML = "";
        const filt = getFilters();
        const binEnd = binStart + BIN_SIZE - 1;
        const visible = entries
            .filter((e) => entryPasses(e, filt))
            .filter((e) => e.year != null && e.year >= binStart && e.year <= binEnd);

        document.getElementById("visibleCount").textContent = visible.length;
        if (visible.length === 0) {
            renderEmpty(svg, `No entries for ${binStart}–${binEnd} match current filters.`);
            updatePageControls(1);
            return;
        }

        const PAD_LEFT = 80;
        const PAD_RIGHT = 60;
        const AXIS_Y = 50;
        const ROW_HEIGHT = 30;
        const ROW_TOP = AXIS_Y + 22;
        const W = getContainerWidth();
        const usable = W - PAD_LEFT - PAD_RIGHT;
        const yearToX = (year) => PAD_LEFT + ((year - binStart + 0.5) / BIN_SIZE) * usable;

        // Axis
        const axisGroup = document.createElementNS(SVG_NS, "g");
        axisGroup.setAttribute("class", "tl-axis");
        const axisLine = document.createElementNS(SVG_NS, "line");
        axisLine.setAttribute("x1", PAD_LEFT);
        axisLine.setAttribute("x2", W - PAD_RIGHT);
        axisLine.setAttribute("y1", AXIS_Y);
        axisLine.setAttribute("y2", AXIS_Y);
        axisLine.setAttribute("stroke", "#3a3a4f");
        axisGroup.appendChild(axisLine);
        for (let y = binStart; y <= binEnd; y++) {
            const x = yearToX(y);
            const tick = document.createElementNS(SVG_NS, "line");
            tick.setAttribute("x1", x);
            tick.setAttribute("x2", x);
            tick.setAttribute("y1", AXIS_Y - 5);
            tick.setAttribute("y2", AXIS_Y + 5);
            tick.setAttribute("stroke", "#5a5a6e");
            axisGroup.appendChild(tick);
            const label = document.createElementNS(SVG_NS, "text");
            label.setAttribute("class", "tl-year-label tl-year-clickable");
            label.setAttribute("x", x);
            label.setAttribute("y", AXIS_Y - 10);
            label.setAttribute("text-anchor", "middle");
            label.textContent = y;
            label.addEventListener("click", () => setModeYearDetail(y));
            label.style.cursor = "pointer";
            axisGroup.appendChild(label);
        }
        const binTitle = document.createElementNS(SVG_NS, "text");
        binTitle.setAttribute("class", "tl-year-bigtitle");
        binTitle.setAttribute("x", 8);
        binTitle.setAttribute("y", AXIS_Y - 8);
        binTitle.textContent = `${binStart}–${binEnd}`;
        axisGroup.appendChild(binTitle);
        svg.appendChild(axisGroup);

        // Top-N per year
        const byYear = {};
        visible.forEach((e) => {
            (byYear[e.year] = byYear[e.year] || []).push(e);
        });
        let maxRowsAny = 0;
        const sortedYears = Object.keys(byYear).map(Number).sort((a, b) => a - b);
        sortedYears.forEach((y) => {
            const list = byYear[y].slice().sort(curationOrder);
            const shown = list.slice(0, BUDGET_OVERVIEW_PER_YEAR);
            const hidden = list.length - shown.length;
            const x = yearToX(y);
            shown.forEach((e, i) => {
                maxRowsAny = Math.max(maxRowsAny, i + 1);
                const yPx = ROW_TOP + i * ROW_HEIGHT;
                drawEntry(svg, e, x, yPx, /*showLabel*/ true);
            });
            if (hidden > 0) {
                const row = shown.length;
                maxRowsAny = Math.max(maxRowsAny, row + 1);
                const yPx = ROW_TOP + row * ROW_HEIGHT;
                drawMoreBadge(svg, x, yPx, hidden, () => setModeYearDetail(y),
                    `Showing ${shown.length} of ${list.length} for ${y} — click to zoom in`);
            }
        });

        finalizeViewBox(svg, W, ROW_TOP, maxRowsAny, ROW_HEIGHT);
        updatePageControls(1);
    }

    // ── Year-detail mode (one year, month spread) ──────────────────────────
    function renderYearDetail(year) {
        const svg = document.getElementById("timelineSvg");
        svg.innerHTML = "";
        const filt = getFilters();
        const yearEntries = entries
            .filter((e) => entryPasses(e, filt))
            .filter((e) => e.year === year);

        document.getElementById("visibleCount").textContent = yearEntries.length;
        if (yearEntries.length === 0) {
            renderEmpty(svg, `No entries for ${year} match current filters.`);
            updatePageControls(1);
            return;
        }

        const PAD_LEFT = 80;
        const PAD_RIGHT = 60;
        const AXIS_Y = 50;
        const ROW_HEIGHT = 30;
        const ROW_TOP = AXIS_Y + 22;
        const W = getContainerWidth();
        const usable = W - PAD_LEFT - PAD_RIGHT;
        const monthToX = (m) => PAD_LEFT + (m - 0.5) / 12 * usable;

        const byMonth = {};
        yearEntries.forEach((e) => {
            const m = parseMonth(e.date_iso);
            (byMonth[m] = byMonth[m] || []).push(e);
        });

        const axisGroup = document.createElementNS(SVG_NS, "g");
        axisGroup.setAttribute("class", "tl-axis");
        const axisLine = document.createElementNS(SVG_NS, "line");
        axisLine.setAttribute("x1", PAD_LEFT);
        axisLine.setAttribute("x2", W - PAD_RIGHT);
        axisLine.setAttribute("y1", AXIS_Y);
        axisLine.setAttribute("y2", AXIS_Y);
        axisLine.setAttribute("stroke", "#3a3a4f");
        axisGroup.appendChild(axisLine);

        const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
        for (let m = 1; m <= 12; m++) {
            const x = monthToX(m);
            const tick = document.createElementNS(SVG_NS, "line");
            tick.setAttribute("x1", x);
            tick.setAttribute("x2", x);
            tick.setAttribute("y1", AXIS_Y - 5);
            tick.setAttribute("y2", AXIS_Y + 5);
            tick.setAttribute("stroke", "#5a5a6e");
            axisGroup.appendChild(tick);
            const label = document.createElementNS(SVG_NS, "text");
            label.setAttribute("x", x);
            label.setAttribute("y", AXIS_Y - 10);
            label.setAttribute("text-anchor", "middle");
            label.textContent = monthNames[m - 1];
            // Months with entries zoom into day-level detail, mirroring the
            // clickable year labels in overview.
            if (byMonth[m]) {
                label.setAttribute("class", "tl-year-label tl-year-clickable");
                label.addEventListener("click", () => setModeMonthDetail(year, m));
                label.style.cursor = "pointer";
            } else {
                label.setAttribute("class", "tl-year-label");
            }
            axisGroup.appendChild(label);
        }
        const yearText = document.createElementNS(SVG_NS, "text");
        yearText.setAttribute("class", "tl-year-bigtitle");
        yearText.setAttribute("x", 8);
        yearText.setAttribute("y", AXIS_Y - 8);
        yearText.textContent = String(year);
        axisGroup.appendChild(yearText);
        svg.appendChild(axisGroup);

        let maxRowsAny = 0;
        Object.keys(byMonth).forEach((mKey) => {
            const m = Number(mKey);
            // Curation order so the budget keeps the most-important items,
            // not just the earliest in the month; date order is reserved for
            // the month-detail view where everything fits.
            const list = byMonth[m].slice().sort(curationOrder);
            const expanded = expandedMonths.has(m);
            const shown = expanded ? list : list.slice(0, BUDGET_YEAR_PER_MONTH);
            const hidden = list.length - shown.length;
            const x = m >= 1 && m <= 12 ? monthToX(m) : PAD_LEFT - 20;
            shown.forEach((e, i) => {
                maxRowsAny = Math.max(maxRowsAny, i + 1);
                const yPx = ROW_TOP + i * ROW_HEIGHT;
                drawEntry(svg, e, x, yPx, /*showLabel*/ true);
            });
            if (hidden > 0) {
                const row = shown.length;
                maxRowsAny = Math.max(maxRowsAny, row + 1);
                const yPx = ROW_TOP + row * ROW_HEIGHT;
                drawMoreBadge(svg, x, yPx, hidden,
                    () => { expandedMonths.add(m); render(); },
                    `Showing ${shown.length} of ${list.length} — click to show all`);
            } else if (expanded && list.length > BUDGET_YEAR_PER_MONTH) {
                const row = shown.length;
                maxRowsAny = Math.max(maxRowsAny, row + 1);
                const yPx = ROW_TOP + row * ROW_HEIGHT;
                drawMoreBadge(svg, x, yPx, 0,
                    () => { expandedMonths.delete(m); render(); },
                    `Showing all ${list.length} — click to show top ${BUDGET_YEAR_PER_MONTH}`,
                    /*labelOverride*/ "show less", /*extraClass*/ "tl-collapse");
            }
        });

        finalizeViewBox(svg, W, ROW_TOP, maxRowsAny, ROW_HEIGHT);
        updatePageControls(1);
    }

    // ── Month-detail mode (one month, day spread, no cap) ──────────────────
    function renderMonthDetail(year, month) {
        const svg = document.getElementById("timelineSvg");
        svg.innerHTML = "";
        const filt = getFilters();
        const monthEntries = entries
            .filter((e) => entryPasses(e, filt))
            .filter((e) => e.year === year && parseMonth(e.date_iso) === month);

        document.getElementById("visibleCount").textContent = monthEntries.length;
        if (monthEntries.length === 0) {
            const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
            renderEmpty(svg, `No entries for ${monthNames[month - 1]} ${year} match current filters.`);
            updatePageControls(1);
            return;
        }

        const PAD_LEFT = 80;
        const PAD_RIGHT = 60;
        const AXIS_Y = 50;
        const ROW_HEIGHT = 30;
        const ROW_TOP = AXIS_Y + 22;
        const W = getContainerWidth();
        const usable = W - PAD_LEFT - PAD_RIGHT;
        const daysInMonth = new Date(year, month, 0).getDate();
        const dayToX = (d) => PAD_LEFT + (d - 0.5) / daysInMonth * usable;

        // Axis (day ticks)
        const axisGroup = document.createElementNS(SVG_NS, "g");
        axisGroup.setAttribute("class", "tl-axis");
        const axisLine = document.createElementNS(SVG_NS, "line");
        axisLine.setAttribute("x1", PAD_LEFT);
        axisLine.setAttribute("x2", W - PAD_RIGHT);
        axisLine.setAttribute("y1", AXIS_Y);
        axisLine.setAttribute("y2", AXIS_Y);
        axisLine.setAttribute("stroke", "#3a3a4f");
        axisGroup.appendChild(axisLine);
        // Label every 5th day to keep the axis readable.
        for (let d = 1; d <= daysInMonth; d++) {
            const x = dayToX(d);
            const tick = document.createElementNS(SVG_NS, "line");
            tick.setAttribute("x1", x);
            tick.setAttribute("x2", x);
            tick.setAttribute("y1", AXIS_Y - 4);
            tick.setAttribute("y2", AXIS_Y + 4);
            tick.setAttribute("stroke", "#5a5a6e");
            axisGroup.appendChild(tick);
            if (d === 1 || d % 5 === 0 || d === daysInMonth) {
                const label = document.createElementNS(SVG_NS, "text");
                label.setAttribute("class", "tl-year-label");
                label.setAttribute("x", x);
                label.setAttribute("y", AXIS_Y - 10);
                label.setAttribute("text-anchor", "middle");
                label.textContent = d;
                axisGroup.appendChild(label);
            }
        }
        const monthNames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
        const title = document.createElementNS(SVG_NS, "text");
        title.setAttribute("class", "tl-year-bigtitle");
        title.setAttribute("x", 8);
        title.setAttribute("y", AXIS_Y - 8);
        title.textContent = `${monthNames[month - 1]} ${year}`;
        axisGroup.appendChild(title);
        svg.appendChild(axisGroup);

        // Spread by day, stack within day. No cap — month-detail shows everything.
        const byDay = {};
        monthEntries.forEach((e) => {
            const d = parseDay(e.date_iso) || 0;
            (byDay[d] = byDay[d] || []).push(e);
        });

        let maxRowsAny = 0;
        Object.keys(byDay).forEach((dKey) => {
            const d = Number(dKey);
            const list = byDay[d].slice().sort(curationOrder);
            const x = d >= 1 && d <= daysInMonth ? dayToX(d) : PAD_LEFT - 20;
            list.forEach((e, i) => {
                maxRowsAny = Math.max(maxRowsAny, i + 1);
                const yPx = ROW_TOP + i * ROW_HEIGHT;
                drawEntry(svg, e, x, yPx, /*showLabel*/ true);
            });
        });

        finalizeViewBox(svg, W, ROW_TOP, maxRowsAny, ROW_HEIGHT);
        updatePageControls(1);
    }

    // ── Axis helpers ───────────────────────────────────────────────────────
    function renderAxisYears(svg, yearMin, yearMax, yearToX, byYear, onYearClick) {
        const axisGroup = document.createElementNS(SVG_NS, "g");
        axisGroup.setAttribute("class", "tl-axis");
        const AXIS_Y = 40;
        const PAD_LEFT = 60;
        const W = parseFloat(svg.style.width) || 0; // not set yet
        // We don't know W here yet; just iterate years.
        const span = yearMax - yearMin;
        const yearStep = span > 30 ? 5 : 1;
        const axisLine = document.createElementNS(SVG_NS, "line");
        axisLine.setAttribute("x1", PAD_LEFT);
        axisLine.setAttribute("x2", yearToX(yearMax) + 20);
        axisLine.setAttribute("y1", AXIS_Y);
        axisLine.setAttribute("y2", AXIS_Y);
        axisLine.setAttribute("stroke", "#3a3a4f");
        axisGroup.appendChild(axisLine);
        for (let y = Math.ceil(yearMin / yearStep) * yearStep; y <= yearMax; y += yearStep) {
            const x = yearToX(y);
            const tick = document.createElementNS(SVG_NS, "line");
            tick.setAttribute("x1", x);
            tick.setAttribute("x2", x);
            tick.setAttribute("y1", AXIS_Y - 5);
            tick.setAttribute("y2", AXIS_Y + 5);
            tick.setAttribute("stroke", "#5a5a6e");
            axisGroup.appendChild(tick);
            const label = document.createElementNS(SVG_NS, "text");
            label.setAttribute("class", "tl-year-label tl-year-clickable");
            label.setAttribute("x", x);
            label.setAttribute("y", AXIS_Y - 10);
            label.setAttribute("text-anchor", "middle");
            label.textContent = y;
            if (byYear[y]) {
                label.addEventListener("click", () => onYearClick(y));
                label.style.cursor = "pointer";
            }
            axisGroup.appendChild(label);
        }
        svg.appendChild(axisGroup);
    }

    function renderAxisBins(svg, binStart, binEnd, binToX, byBin) {
        const axisGroup = document.createElementNS(SVG_NS, "g");
        axisGroup.setAttribute("class", "tl-axis");
        const AXIS_Y = 40;
        const PAD_LEFT = 60;
        const axisLine = document.createElementNS(SVG_NS, "line");
        axisLine.setAttribute("x1", PAD_LEFT);
        axisLine.setAttribute("x2", binToX(binEnd - BIN_SIZE + 1) + 20);
        axisLine.setAttribute("y1", AXIS_Y);
        axisLine.setAttribute("y2", AXIS_Y);
        axisLine.setAttribute("stroke", "#3a3a4f");
        axisGroup.appendChild(axisLine);
        for (let b = binStart; b <= binEnd - BIN_SIZE + 1; b += BIN_SIZE) {
            const x = binToX(b);
            const tick = document.createElementNS(SVG_NS, "line");
            tick.setAttribute("x1", x);
            tick.setAttribute("x2", x);
            tick.setAttribute("y1", AXIS_Y - 5);
            tick.setAttribute("y2", AXIS_Y + 5);
            tick.setAttribute("stroke", "#5a5a6e");
            axisGroup.appendChild(tick);
            const label = document.createElementNS(SVG_NS, "text");
            label.setAttribute("class", "tl-year-label tl-year-clickable");
            label.setAttribute("x", x);
            label.setAttribute("y", AXIS_Y - 10);
            label.setAttribute("text-anchor", "middle");
            label.textContent = `${b}–${(b + BIN_SIZE - 1) % 100}`.padStart(2, "0");
            // Show full bin label: e.g. "1975–79"
            label.textContent = `${b}–${String((b + BIN_SIZE - 1) % 100).padStart(2, "0")}`;
            if (byBin[b]) {
                label.addEventListener("click", () => setModeBinDetail(b));
                label.style.cursor = "pointer";
            }
            axisGroup.appendChild(label);
        }
        svg.appendChild(axisGroup);
    }

    // Undated lane — capped labels + "+N more undated" badge
    function renderUndatedLane(svg, proseOnly, W, PAD_LEFT, PAD_RIGHT, ROW_TOP, ROW_HEIGHT, maxRowsAny) {
        if (proseOnly.length === 0) return maxRowsAny;
        const laneRow = maxRowsAny + 1;
        const laneY = ROW_TOP + laneRow * ROW_HEIGHT;
        const laneText = document.createElementNS(SVG_NS, "text");
        laneText.setAttribute("class", "tl-lane-label");
        laneText.setAttribute("x", 8);
        laneText.setAttribute("y", laneY + 4);
        laneText.textContent = "Undated:";
        svg.appendChild(laneText);

        const sorted = proseOnly.slice().sort(curationOrder);
        const labelable = sorted.slice(0, UNDATED_LABEL_CAP);
        const rest = sorted.slice(UNDATED_LABEL_CAP);
        const usable = W - PAD_LEFT - PAD_RIGHT - 80; // reserve room for "+N undated" at right
        const totalShown = labelable.length;

        labelable.forEach((e, i) => {
            const ratio = totalShown === 1 ? 0.5 : i / (totalShown - 1);
            const x = PAD_LEFT + ratio * usable;
            drawEntry(svg, e, x, laneY, /*showLabel*/ true);
        });

        if (rest.length > 0) {
            const x = W - PAD_RIGHT - 30;
            drawMoreBadge(svg, x, laneY, rest.length, null,
                `${rest.length} more undated (raise importance filter or open detail)`,
                /*labelOverride*/ `+${rest.length} undated`);
        }
        return laneRow + 1;
    }

    function finalizeViewBox(svg, W, ROW_TOP, maxRowsAny, ROW_HEIGHT) {
        const totalHeight = ROW_TOP + Math.max(1, maxRowsAny) * ROW_HEIGHT + 30;
        svg.setAttribute("viewBox", `0 0 ${W} ${totalHeight}`);
        svg.style.width = `${W}px`;
        svg.style.height = `${totalHeight}px`;
    }

    function parseMonth(iso) {
        if (!iso || iso.length < 7) return 0;
        const m = parseInt(iso.slice(5, 7), 10);
        return Number.isFinite(m) ? m : 0;
    }
    function parseDay(iso) {
        if (!iso || iso.length < 10) return 0;
        const d = parseInt(iso.slice(8, 10), 10);
        return Number.isFinite(d) ? d : 0;
    }

    function drawEntry(svg, e, x, y, showLabel) {
        const color = colorForCategory(e.category);
        const r = dotRadius(e.importance);
        const op = dotOpacity(e.importance);

        const dot = document.createElementNS(SVG_NS, "circle");
        dot.setAttribute("cx", x);
        dot.setAttribute("cy", y);
        dot.setAttribute("r", r);
        dot.setAttribute("fill", color);
        dot.setAttribute("opacity", op);
        dot.setAttribute("class", "tl-event");
        const dashes = strokeForConfidence(e.date_confidence);
        if (dashes) {
            dot.setAttribute("stroke", color);
            dot.setAttribute("stroke-width", 1.5);
            dot.setAttribute("stroke-dasharray", dashes);
            dot.setAttribute("fill-opacity", op * 0.4);
        }
        dot.addEventListener("click", () => showDetail(e));
        const title = document.createElementNS(SVG_NS, "title");
        title.textContent = `${e.date_display || e.date_iso || e.date_prose || "?"} · ${e.label}${e.importance != null ? " · imp " + e.importance.toFixed(1) : ""}\n${e.sentence || ""}`;
        dot.appendChild(title);
        svg.appendChild(dot);

        if (showLabel) {
            // Label sits UNDER the dot, centered horizontally. Narrow column,
            // tight truncation. Full text on hover (the <title> on the dot
            // already carries the verbatim label + sentence).
            const lbl = document.createElementNS(SVG_NS, "text");
            lbl.setAttribute("class", "tl-event-label");
            lbl.setAttribute("x", x);
            lbl.setAttribute("y", y + r + 10);
            lbl.setAttribute("text-anchor", "middle");
            lbl.textContent = e.label.length > 18 ? e.label.slice(0, 16) + "…" : e.label;
            // Tooltip with full text — duplicates the dot's title so hovering
            // the label also works.
            const lblTitle = document.createElementNS(SVG_NS, "title");
            lblTitle.textContent = `${e.date_display || e.date_iso || e.date_prose || "?"} · ${e.label}${e.importance != null ? " · imp " + e.importance.toFixed(1) : ""}`;
            lbl.appendChild(lblTitle);
            lbl.addEventListener("click", () => showDetail(e));
            lbl.style.cursor = "pointer";
            svg.appendChild(lbl);
        }
    }

    function drawMoreBadge(svg, x, y, count, onClick, tooltip, labelOverride, extraClass) {
        const text = labelOverride || `+${count} more`;
        // Width grows with label
        const w = Math.max(48, text.length * 7 + 14);
        const g = document.createElementNS(SVG_NS, "g");
        g.setAttribute("class", extraClass ? `tl-more-badge ${extraClass}` : "tl-more-badge");
        g.setAttribute("transform", `translate(${x - w / 2}, ${y - 9})`);
        if (onClick) g.style.cursor = "pointer";

        const rect = document.createElementNS(SVG_NS, "rect");
        rect.setAttribute("x", 0);
        rect.setAttribute("y", 0);
        rect.setAttribute("width", w);
        rect.setAttribute("height", 18);
        rect.setAttribute("rx", 9);
        rect.setAttribute("fill", "#2a2a3a");
        rect.setAttribute("stroke", "#5a5a6e");
        g.appendChild(rect);

        const t = document.createElementNS(SVG_NS, "text");
        t.setAttribute("x", w / 2);
        t.setAttribute("y", 13);
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("class", "tl-more-text");
        t.textContent = text;
        g.appendChild(t);

        if (tooltip) {
            const title = document.createElementNS(SVG_NS, "title");
            title.textContent = tooltip;
            g.appendChild(title);
        }

        if (onClick) g.addEventListener("click", onClick);
        svg.appendChild(g);
    }

    function renderEmpty(svg, msg) {
        const t = document.createElementNS(SVG_NS, "text");
        t.setAttribute("x", 20);
        t.setAttribute("y", 40);
        t.setAttribute("fill", "#6f6f88");
        t.textContent = msg;
        svg.appendChild(t);
        svg.setAttribute("viewBox", "0 0 800 80");
        svg.style.height = "80px";
    }

    window.showDetail = function (e) {
        const panel = document.getElementById("detailPanel");
        const c = document.getElementById("detailContent");
        const conf = e.date_confidence ? ` (${e.date_confidence})` : "";
        const dateStr = e.date_display || e.date_iso || e.date_prose || "—";
        const endStr = e.end_iso || e.end_prose || "";
        c.innerHTML = `
            <h3>${escapeHtml(e.label)}</h3>
            <div class="detail-date">${dateStr}${endStr && endStr !== dateStr ? " → " + endStr : ""}${conf}</div>
            <div class="detail-sentence">${escapeHtml(e.sentence || "(no sentence)")}</div>
            <div class="detail-meta">
                <span><strong>Type:</strong> ${escapeHtml(e.node_type)}</span>
                ${e.category ? `<span><strong>Category:</strong> ${escapeHtml(e.category)}</span>` : ""}
                ${e.importance != null ? `<span><strong>Importance:</strong> ${e.importance.toFixed(1)} / 10</span>` : ""}
                <span><strong>Relationship:</strong> ${escapeHtml(e.relationship_type)} (${e.direction})</span>
            </div>
        `;
        panel.classList.remove("hidden");
    };

    window.hideDetail = function () {
        document.getElementById("detailPanel").classList.add("hidden");
    };

    window.backToOverview = stepBack;

    function escapeHtml(s) {
        return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    let resizeTimer = null;
    document.addEventListener("DOMContentLoaded", function () {
        ["impFilter", "showEstimated", "showProseOnly"].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.addEventListener("input", render);
        });
        window.addEventListener("resize", () => {
            if (resizeTimer) clearTimeout(resizeTimer);
            resizeTimer = setTimeout(render, 120);
        });
        loadData();
    });
})();
