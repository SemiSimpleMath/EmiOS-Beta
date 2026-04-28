// Wellness Reminders settings

let config = null;

document.addEventListener('DOMContentLoaded', function() {
    load();
});

async function load() {
    const resp = await fetch('/api/settings/wellness');
    const data = await resp.json();
    if (!data.success) { showStatus('Failed to load config', true); return; }
    config = data.config;
    render();
}

function render() {
    const list = document.getElementById('activities-list');
    list.innerHTML = '';
    const activities = config.activities || {};
    for (const [key, act] of Object.entries(activities)) {
        list.appendChild(makeCard(key, act));
    }
}

function makeCard(key, act) {
    const card = document.createElement('div');
    card.className = 'activity-card';
    card.dataset.key = key;
    const mins = act.threshold ? act.threshold.minutes : '';
    const label = act.threshold ? act.threshold.label : '';
    card.innerHTML = `
        <div class="activity-header">
            <h3>${escapeHtml(act.display_name || key)}</h3>
            <button type="button" class="btn-remove" title="Remove activity">&times;</button>
        </div>
        <div class="activity-fields">
            <div>
                <label>Display Name</label>
                <input type="text" class="f-display" value="${escapeHtml(act.display_name || '')}">
            </div>
            <div>
                <label>Reminder interval (minutes)</label>
                <input type="number" class="f-minutes" min="5" step="5" value="${mins}">
            </div>
            <div>
                <label>Description</label>
                <input type="text" class="f-label" value="${escapeHtml(label)}">
            </div>
            <div>
                <label>Guidance / notes</label>
                <input type="text" class="f-guidance" value="${escapeHtml(act.guidance || '')}" placeholder="e.g., Typical windows: 7-9am, 11am-1pm">
            </div>
            <div>
                <label>Track daily count</label>
                <select class="f-count">
                    <option value="true" ${act.show_daily_count ? 'selected' : ''}>Yes</option>
                    <option value="false" ${!act.show_daily_count ? 'selected' : ''}>No</option>
                </select>
            </div>
            <div>
                <label>Reset timer on AFK return</label>
                <select class="f-reset-afk">
                    <option value="true" ${act.reset_on_afk ? 'selected' : ''}>Yes</option>
                    <option value="false" ${!act.reset_on_afk ? 'selected' : ''}>No</option>
                </select>
            </div>
            <div>
                <label>Start reminder on app launch</label>
                <select class="f-cold-start">
                    <option value="true" ${act.init_on_cold_start ? 'selected' : ''}>Yes</option>
                    <option value="false" ${!act.init_on_cold_start ? 'selected' : ''}>No</option>
                </select>
            </div>
        </div>
    `;
    card.querySelector('.btn-remove').addEventListener('click', () => {
        delete config.activities[key];
        render();
    });
    return card;
}

document.getElementById('add-activity-btn').addEventListener('click', () => {
    const id = 'custom_' + Date.now();
    config.activities[id] = {
        display_name: 'New Reminder',
        field_name: id,
        init_on_cold_start: false,
        reset_on_afk: false,
        show_daily_count: true,
        threshold: { label: '', minutes: 60 }
    };
    render();
    const cards = document.querySelectorAll('.activity-card');
    const last = cards[cards.length - 1];
    if (last) last.querySelector('.f-display').focus();
});

document.getElementById('save-btn').addEventListener('click', async () => {
    const cards = document.querySelectorAll('.activity-card');
    const activities = {};
    cards.forEach(card => {
        const key = card.dataset.key;
        const orig = config.activities[key] || {};
        const displayName = card.querySelector('.f-display').value.trim() || key;
        const minutes = parseInt(card.querySelector('.f-minutes').value) || 0;
        const label = card.querySelector('.f-label').value.trim();
        const showCount = card.querySelector('.f-count').value === 'true';
        const resetAfk = card.querySelector('.f-reset-afk').value === 'true';
        const coldStart = card.querySelector('.f-cold-start').value === 'true';
        const guidance = card.querySelector('.f-guidance').value.trim();
        const fieldName = orig.field_name || key.toLowerCase().replace(/\s+/g, '_');
        const act = {
            ...orig,
            display_name: displayName,
            field_name: fieldName,
            show_daily_count: showCount,
            reset_on_afk: resetAfk,
            init_on_cold_start: coldStart,
        };
        if (guidance) {
            act.guidance = guidance;
        } else {
            delete act.guidance;
        }
        if (minutes > 0) {
            act.threshold = { label: label || `Every ${minutes} minutes`, minutes };
        } else {
            delete act.threshold;
        }
        activities[fieldName] = act;
    });
    try {
        const resp = await fetch('/api/settings/wellness', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ activities })
        });
        const data = await resp.json();
        if (data.success) {
            config = data.config;
            render();
            showStatus('Saved!', false);
        } else {
            showStatus('Save failed: ' + (data.message || 'unknown error'), true);
        }
    } catch (e) {
        showStatus('Save failed: ' + e.message, true);
    }
});

function showStatus(msg, isError) {
    const el = document.getElementById('status-msg');
    el.textContent = msg;
    el.className = 'status-msg ' + (isError ? 'status-err' : 'status-ok');
    if (!isError) setTimeout(() => { el.style.display = 'none'; }, 3000);
}

function escapeHtml(str) {
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}
