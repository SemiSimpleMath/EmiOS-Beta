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

    function renderSnapshots(snapshots) {
        grid.innerHTML = '';
        if (!snapshots || snapshots.length === 0) {
            empty.style.display = 'block';
            countEl.textContent = '';
            return;
        }
        empty.style.display = 'none';
        countEl.textContent = snapshots.length + ' snapshot' + (snapshots.length === 1 ? '' : 's');
        const frag = document.createDocumentFragment();
        snapshots.forEach(function (snap) {
            const captionText = formatTimestamp(snap.captured_at_utc) + ' · ' + (snap.camera_id || '');
            const card = document.createElement('div');
            card.className = 'ring-snapshot-card';
            card.innerHTML =
                '<img loading="lazy" src="/api/ring/snapshots/image/' + encodeURIComponent(snap.filename)
              + '" alt="' + escapeHtml(captionText) + '">'
              + '<div class="ring-snapshot-card-meta">'
              + '<div class="ring-snapshot-card-camera">' + escapeHtml(snap.camera_id || 'unknown') + '</div>'
              + '<div class="ring-snapshot-card-time">' + escapeHtml(formatTimestamp(snap.captured_at_utc)) + '</div>'
              + '<div class="ring-snapshot-card-size">' + escapeHtml(formatBytes(snap.size_bytes)) + '</div>'
              + '</div>';
            card.addEventListener('click', function () { openLightbox(snap.filename, captionText); });
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
            renderSnapshots(data.snapshots || []);
        } catch (err) {
            showError('Failed to load snapshots: ' + (err.message || err));
        }
    }

    refreshBtn.addEventListener('click', loadSnapshots);
    loadSnapshots();
})();
