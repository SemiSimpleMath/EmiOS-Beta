// Insights page — Daily / Weekly / Beliefs viewer

document.addEventListener('DOMContentLoaded', function() {
    initTabs();
    initDaily();
    initWeekly();
    initBeliefs();
});

// ---- Tab switching ----
function initTabs() {
    document.querySelectorAll('.insights-tab').forEach(function(tab) {
        tab.addEventListener('click', function() {
            document.querySelectorAll('.insights-tab').forEach(function(t) { t.classList.remove('active'); });
            document.querySelectorAll('.insights-panel').forEach(function(p) { p.classList.remove('active'); });
            tab.classList.add('active');
            var panel = document.getElementById('panel-' + tab.dataset.tab);
            if (panel) panel.classList.add('active');
        });
    });
}

// ---- Markdown renderer ----
function renderMd(text) {
    if (typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined') {
        try { return DOMPurify.sanitize(marked.parse(String(text || ''))); } catch(e) {}
    }
    return String(text || '').replace(/\n/g, '<br>');
}

function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = String(s || '');
    return d.innerHTML;
}

// ---- Daily ----
var dailyDate = new Date();

function initDaily() {
    var picker = document.getElementById('daily-date-picker');
    picker.value = toDateStr(dailyDate);
    picker.addEventListener('change', function() {
        dailyDate = new Date(picker.value + 'T12:00:00');
        loadDaily();
    });
    document.getElementById('daily-prev').addEventListener('click', function() {
        dailyDate.setDate(dailyDate.getDate() - 1);
        picker.value = toDateStr(dailyDate);
        loadDaily();
    });
    document.getElementById('daily-next').addEventListener('click', function() {
        dailyDate.setDate(dailyDate.getDate() + 1);
        picker.value = toDateStr(dailyDate);
        loadDaily();
    });
    loadDaily();
}

function loadDaily() {
    var el = document.getElementById('daily-content');
    el.innerHTML = '<div class="insights-loading">Loading...</div>';
    fetch('/api/insights/daily?date=' + toDateStr(dailyDate))
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.success) {
                el.innerHTML = '<div class="insights-empty">' + escHtml(data.message || 'No insights for this date.') + '</div>';
                return;
            }
            renderDailyContent(el, data);
        })
        .catch(function(e) {
            el.innerHTML = '<div class="insights-empty">Failed to load: ' + escHtml(e.message) + '</div>';
        });
}

function renderDailyContent(el, data) {
    var s = data.summary || {};
    var parts = [];

    parts.push('<h2>' + escHtml(s.title || 'Daily Assessment') + '</h2>');
    parts.push('<p style="color:#8899aa;font-size:0.82rem;">' + escHtml(data.date || '') + '</p>');

    if (s.executive_summary && s.executive_summary.length) {
        parts.push('<h3>Summary</h3><ul>');
        s.executive_summary.forEach(function(item) {
            parts.push('<li>' + escHtml(item) + '</li>');
        });
        parts.push('</ul>');
    }

    if (s.dominant_themes && s.dominant_themes.length) {
        parts.push('<h3>Themes</h3>');
        s.dominant_themes.forEach(function(theme) {
            parts.push('<div class="theme-card">');
            parts.push('<h4>' + escHtml(theme.theme || '') + '</h4>');
            if (theme.why_it_matters) {
                parts.push('<p>' + escHtml(theme.why_it_matters) + '</p>');
            }
            if (theme.evidence && theme.evidence.length) {
                parts.push('<div class="theme-evidence">');
                theme.evidence.forEach(function(ev) {
                    var ref = (typeof ev === 'string') ? ev : (ev.ref || ev.text || JSON.stringify(ev));
                    parts.push('<div>- ' + escHtml(ref) + '</div>');
                });
                parts.push('</div>');
            }
            parts.push('</div>');
        });
    }

    if (s.belief_candidates && s.belief_candidates.length) {
        parts.push('<h3>Belief Candidates</h3><ul>');
        s.belief_candidates.forEach(function(b) {
            var text = (typeof b === 'string') ? b : (b.statement || b.text || JSON.stringify(b));
            parts.push('<li>' + escHtml(text) + '</li>');
        });
        parts.push('</ul>');
    }

    if (s.open_questions && s.open_questions.length) {
        parts.push('<h3>Open Questions</h3><ul>');
        s.open_questions.forEach(function(q) {
            parts.push('<li>' + escHtml(typeof q === 'string' ? q : q.question || JSON.stringify(q)) + '</li>');
        });
        parts.push('</ul>');
    }

    // Daily learnings (actionable_information from resource_daily_insights.json)
    var learnings = data.learnings || [];
    if (learnings.length) {
        parts.push('<h3>What Emi Learned Today</h3>');
        learnings.forEach(function(item) {
            parts.push('<div class="theme-card">');
            parts.push('<h4>' + escHtml(item.fact_summary || '') + '</h4>');
            if (item.tags && item.tags.length) {
                parts.push('<div class="theme-meta">');
                item.tags.forEach(function(tag) {
                    parts.push('<span class="belief-domain">' + escHtml(tag) + '</span> ');
                });
                if (item.temporal_scope) parts.push('<span style="font-size:0.7rem;color:#8899aa;margin-left:0.5rem;">' + escHtml(item.temporal_scope) + '</span>');
                parts.push('</div>');
            }
            if (item.change_recommended) {
                parts.push('<p>' + escHtml(item.change_recommended) + '</p>');
            }
            if (item.evidence && item.evidence.length) {
                parts.push('<div class="theme-evidence">');
                item.evidence.forEach(function(ev) {
                    parts.push('<div>- ' + escHtml(typeof ev === 'string' ? ev : JSON.stringify(ev)) + '</div>');
                });
                parts.push('</div>');
            }
            parts.push('</div>');
        });
    }

    el.innerHTML = parts.join('');
}

// ---- Weekly ----
var weekOffset = 0;

function initWeekly() {
    document.getElementById('weekly-prev').addEventListener('click', function() {
        weekOffset--;
        loadWeekly();
    });
    document.getElementById('weekly-next').addEventListener('click', function() {
        weekOffset++;
        loadWeekly();
    });
    loadWeekly();
}

function loadWeekly() {
    var el = document.getElementById('weekly-content');
    var label = document.getElementById('weekly-label');
    el.innerHTML = '<div class="insights-loading">Loading...</div>';

    var refDate = new Date();
    refDate.setDate(refDate.getDate() + (weekOffset * 7));
    var dateStr = toDateStr(refDate);

    fetch('/api/insights/weekly?date=' + dateStr)
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.success) {
                label.textContent = 'Week of ' + dateStr;
                el.innerHTML = '<div class="insights-empty">' + escHtml(data.message || 'No weekly insights found.') + '</div>';
                return;
            }
            label.textContent = escHtml(data.insights.week_label || ('Week of ' + (data.week_start || dateStr)));
            renderWeeklyContent(el, data);
        })
        .catch(function(e) {
            el.innerHTML = '<div class="insights-empty">Failed to load: ' + escHtml(e.message) + '</div>';
        });
}

function renderWeeklyContent(el, data) {
    var ins = data.insights || {};
    var parts = [];

    parts.push('<p style="color:#8899aa;font-size:0.82rem;">' + escHtml(data.week_start || '') + ' to ' + escHtml(data.week_end || '') + '</p>');

    if (ins.executive_summary && ins.executive_summary.length) {
        parts.push('<h3>Executive Summary</h3><ul>');
        ins.executive_summary.forEach(function(item) {
            parts.push('<li>' + escHtml(item) + '</li>');
        });
        parts.push('</ul>');
    }

    if (ins.dominant_themes && ins.dominant_themes.length) {
        parts.push('<h3>Dominant Themes</h3>');
        ins.dominant_themes.forEach(function(theme) {
            parts.push('<div class="theme-card">');
            parts.push('<h4>' + escHtml(theme.theme || '') + '</h4>');
            if (theme.frequency) parts.push('<div class="theme-meta">' + escHtml(theme.frequency) + '</div>');
            if (theme.significance) parts.push('<p>' + escHtml(theme.significance) + '</p>');
            if (theme.evidence && theme.evidence.length) {
                parts.push('<div class="theme-evidence">');
                theme.evidence.forEach(function(ev) {
                    parts.push('<div>- ' + escHtml(typeof ev === 'string' ? ev : JSON.stringify(ev)) + '</div>');
                });
                parts.push('</div>');
            }
            parts.push('</div>');
        });
    }

    if (ins.cross_day_patterns && ins.cross_day_patterns.length) {
        parts.push('<h3>Cross-Day Patterns</h3><ul>');
        ins.cross_day_patterns.forEach(function(p) {
            parts.push('<li>' + escHtml(typeof p === 'string' ? p : p.pattern || JSON.stringify(p)) + '</li>');
        });
        parts.push('</ul>');
    }

    if (ins.belief_candidates && ins.belief_candidates.length) {
        parts.push('<h3>Belief Candidates</h3><ul>');
        ins.belief_candidates.forEach(function(b) {
            parts.push('<li>' + escHtml(typeof b === 'string' ? b : b.statement || JSON.stringify(b)) + '</li>');
        });
        parts.push('</ul>');
    }

    el.innerHTML = parts.join('');
}

// ---- Beliefs ----
var allBeliefs = [];

function initBeliefs() {
    document.getElementById('beliefs-domain-filter').addEventListener('change', renderBeliefs);
    document.getElementById('beliefs-search').addEventListener('input', renderBeliefs);
    loadBeliefs();
}

function loadBeliefs() {
    var el = document.getElementById('beliefs-content');
    el.innerHTML = '<div class="insights-loading">Loading...</div>';
    fetch('/api/insights/beliefs')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            if (!data.success) {
                el.innerHTML = '<div class="insights-empty">' + escHtml(data.message || 'No beliefs found.') + '</div>';
                return;
            }
            allBeliefs = data.beliefs || [];
            renderBeliefs();
        })
        .catch(function(e) {
            el.innerHTML = '<div class="insights-empty">Failed to load: ' + escHtml(e.message) + '</div>';
        });
}

function renderBeliefs() {
    var el = document.getElementById('beliefs-content');
    var domain = document.getElementById('beliefs-domain-filter').value;
    var search = document.getElementById('beliefs-search').value.toLowerCase().trim();

    var filtered = allBeliefs.filter(function(b) {
        if (domain !== 'all' && b.domain !== domain) return false;
        if (search && (b.statement || '').toLowerCase().indexOf(search) === -1
            && (b.belief_key || '').toLowerCase().indexOf(search) === -1) return false;
        return true;
    });

    var parts = [];
    parts.push('<div class="belief-count">Showing ' + filtered.length + ' of ' + allBeliefs.length + ' beliefs</div>');

    filtered.forEach(function(b) {
        parts.push('<div class="belief-card">');
        parts.push('<span class="belief-domain">' + escHtml(b.domain || 'general') + '</span>');
        parts.push('<span class="belief-confidence ' + escHtml(b.confidence || '') + '">' + escHtml(b.confidence || '?') + '</span>');
        parts.push('<span style="font-size:0.7rem;color:#8899aa;">' + escHtml(b.scope || '') + '</span>');
        parts.push('<div class="belief-statement">' + escHtml(b.statement || '') + '</div>');
        parts.push('<div class="belief-meta">');
        if (b.first_observed) parts.push('First seen: ' + escHtml(b.first_observed.substring(0, 10)) + ' &middot; ');
        if (b.last_confirmed) parts.push('Last confirmed: ' + escHtml(b.last_confirmed) + ' &middot; ');
        if (b.observation_count) parts.push('Observations: ' + b.observation_count);
        parts.push('</div></div>');
    });

    el.innerHTML = parts.join('');
}

// ---- Helpers ----
function toDateStr(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
