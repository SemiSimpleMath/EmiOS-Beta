// Ring Snapshots viewer — fetch list, render grid, lightbox on click.

(function () {
    const grid = document.getElementById('ring-snapshots-grid');
    const empty = document.getElementById('ring-snapshots-empty');
    const errorBox = document.getElementById('ring-snapshots-error');
    const countEl = document.getElementById('ring-snapshots-count');
    const refreshBtn = document.getElementById('ring-snapshots-refresh-btn');
    const lightbox = document.getElementById('ring-snapshots-lightbox');
    const lightboxImg = document.getElementById('ring-snapshots-lightbox-img');
    const lightboxCaption = document.getElementById('ring-snapshots-lightbox-caption');
    const lightboxClose = document.getElementById('ring-snapshots-lightbox-close');

    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // Filename ts is "YYYYMMDDTHHMMSSZ" (UTC). Parse → Date → local string.
    function formatTimestamp(ts) {
        if (typeof ts !== 'string' || ts.length !== 16) return ts || '';
        // 20260506T035016Z → 2026-05-06T03:50:16Z
        const iso = ts.slice(0, 4) + '-' + ts.slice(4, 6) + '-' + ts.slice(6, 11)
                  + ':' + ts.slice(11, 13) + ':' + ts.slice(13, 15) + 'Z';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return ts;
        return d.toLocaleString();
    }

    function formatBytes(n) {
        if (typeof n !== 'number' || n <= 0) return '';
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / 1024 / 1024).toFixed(1) + ' MB';
    }

    function showError(msg) {
        errorBox.textContent = msg;
        errorBox.style.display = 'block';
    }

    function hideError() {
        errorBox.textContent = '';
        errorBox.style.display = 'none';
    }

    function openLightbox(filename, captionText) {
        lightboxImg.src = '/api/ring/snapshots/image/' + encodeURIComponent(filename);
        lightboxImg.alt = captionText;
        lightboxCaption.textContent = captionText;
        lightbox.style.display = 'flex';
    }

    function closeLightbox() {
        lightbox.style.display = 'none';
        lightboxImg.src = '';
    }

    lightboxClose.addEventListener('click', closeLightbox);
    lightbox.addEventListener('click', function (ev) {
        if (ev.target === lightbox) closeLightbox();
    });
    document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape' && lightbox.style.display !== 'none') closeLightbox();
    });

    // Map importance 0-10 → bucket label + CSS class.
    function importanceBucket(score) {
        if (typeof score !== 'number' || score < 3) return null;        // boring; no badge
        if (score < 6) return { label: score, cls: 'imp-mild' };       // 3-5 yellow
        if (score < 8) return { label: score, cls: 'imp-notable' };    // 6-7 orange
        return { label: score, cls: 'imp-urgent' };                    // 8-10 red
    }

    let allSnapshots = [];
    let minImportance = 0;

    function renderSnapshots() {
        grid.innerHTML = '';
        const filtered = allSnapshots.filter(function (s) {
            const sc = (s.sidecar && typeof s.sidecar.importance === 'number') ? s.sidecar.importance : 0;
            return sc >= minImportance;
        });
        if (!filtered || filtered.length === 0) {
            empty.style.display = 'block';
            countEl.textContent = allSnapshots.length === 0
                ? ''
                : '0 of ' + allSnapshots.length + ' shown (filter: importance ≥ ' + minImportance + ')';
            return;
        }
        empty.style.display = 'none';
        countEl.textContent = filtered.length + ' of ' + allSnapshots.length
            + ' snapshot' + (allSnapshots.length === 1 ? '' : 's')
            + (minImportance > 0 ? ' (importance ≥ ' + minImportance + ')' : '');
        const frag = document.createDocumentFragment();
        filtered.forEach(function (snap) {
            const sc = snap.sidecar || {};
            const captionPrefix = formatTimestamp(snap.captured_at_utc) + ' · ' + (snap.camera_id || '');
            const captionFull = sc.caption ? captionPrefix + '\n' + sc.caption : captionPrefix;
            const bucket = importanceBucket(sc.importance);

            const card = document.createElement('div');
            card.className = 'ring-snapshot-card';

            let badgeHtml = '';
            if (bucket) {
                const reason = sc.importance_reason || '';
                badgeHtml = '<span class="ring-snapshot-importance ' + bucket.cls + '"'
                    + ' title="' + escapeHtml('importance ' + bucket.label + (reason ? ' — ' + reason : ''))
                    + '">' + bucket.label + '</span>';
            }

            const captionLine = sc.caption
                ? '<div class="ring-snapshot-card-caption" title="' + escapeHtml(sc.caption) + '">'
                  + escapeHtml(sc.caption) + '</div>'
                : '';

            card.innerHTML =
                '<div class="ring-snapshot-img-wrap">'
              + '<img loading="lazy" src="/api/ring/snapshots/image/' + encodeURIComponent(snap.filename)
              + '" alt="' + escapeHtml(captionPrefix) + '">'
              + badgeHtml
              + '</div>'
              + '<div class="ring-snapshot-card-meta">'
              + '<div class="ring-snapshot-card-camera">' + escapeHtml(snap.camera_id || 'unknown') + '</div>'
              + '<div class="ring-snapshot-card-time">' + escapeHtml(formatTimestamp(snap.captured_at_utc)) + '</div>'
              + captionLine
              + '<div class="ring-snapshot-card-size">' + escapeHtml(formatBytes(snap.size_bytes)) + '</div>'
              + '</div>';
            card.addEventListener('click', function () { openLightbox(snap.filename, captionFull); });
            frag.appendChild(card);
        });
        grid.appendChild(frag);
    }

    async function loadSnapshots() {
        hideError();
        try {
            const resp = await fetch('/api/ring/snapshots', { cache: 'no-store' });
            const data = await resp.json();
            if (!data.success) {
                showError(data.error || 'Failed to load snapshots.');
                return;
            }
            allSnapshots = data.snapshots || [];
            renderSnapshots();
        } catch (err) {
            showError('Failed to load snapshots: ' + (err.message || err));
        }
    }

    refreshBtn.addEventListener('click', loadSnapshots);

    // Auto-refresh: handy for watching the 3-min sleep_camera_tick routine
    // pile up frames in real time. Off by default; user opts in.
    let autoRefreshTimer = null;
    const autoRefreshToggle = document.getElementById('ring-snapshots-autorefresh-toggle');
    if (autoRefreshToggle) {
        autoRefreshToggle.addEventListener('change', function () {
            if (autoRefreshTimer) {
                clearInterval(autoRefreshTimer);
                autoRefreshTimer = null;
            }
            if (autoRefreshToggle.checked) {
                autoRefreshTimer = setInterval(loadSnapshots, 15000);
            }
        });
    }

    // Importance filter slider — defined in template; wire if present.
    const filterSlider = document.getElementById('ring-snapshots-importance-filter');
    const filterLabel = document.getElementById('ring-snapshots-importance-label');
    if (filterSlider) {
        filterSlider.addEventListener('input', function () {
            minImportance = parseInt(filterSlider.value, 10) || 0;
            if (filterLabel) {
                filterLabel.textContent = minImportance === 0
                    ? 'all'
                    : '≥ ' + minImportance;
            }
            renderSnapshots();
        });
    }

    loadSnapshots();
})();
