// Voice Settings — standalone page

const OPENAI_VOICES = [
    { id: 'alloy',   label: 'Alloy',   meta: 'Neutral · balanced' },
    { id: 'echo',    label: 'Echo',    meta: 'Male · deep' },
    { id: 'fable',   label: 'Fable',   meta: 'Warm · storytelling' },
    { id: 'nova',    label: 'Nova',    meta: 'Female · energetic' },
    { id: 'onyx',    label: 'Onyx',    meta: 'Male · authoritative' },
    { id: 'shimmer', label: 'Shimmer', meta: 'Female · gentle' },
];

const GEMINI_VOICES = [
    { id: 'Aoede',          label: 'Aoede',          meta: 'Female' },
    { id: 'Callirrhoe',     label: 'Callirrhoe',     meta: 'Female' },
    { id: 'Charon',         label: 'Charon',         meta: 'Male' },
    { id: 'Despina',        label: 'Despina',        meta: 'Female' },
    { id: 'Enceladus',      label: 'Enceladus',      meta: 'Male' },
    { id: 'Erinome',        label: 'Erinome',        meta: 'Female' },
    { id: 'Fenrir',         label: 'Fenrir',         meta: 'Male' },
    { id: 'Gacrux',         label: 'Gacrux',         meta: 'Female' },
    { id: 'Iapetus',        label: 'Iapetus',        meta: 'Male' },
    { id: 'Kore',           label: 'Kore',           meta: 'Female' },
    { id: 'Leda',           label: 'Leda',           meta: 'Female' },
    { id: 'Orus',           label: 'Orus',           meta: 'Male' },
    { id: 'Puck',           label: 'Puck',           meta: 'Male' },
    { id: 'Pulcherrima',    label: 'Pulcherrima',    meta: 'Female' },
    { id: 'Rasalgethi',     label: 'Rasalgethi',     meta: 'Male' },
    { id: 'Sadachbia',      label: 'Sadachbia',      meta: 'Male' },
    { id: 'Schedar',        label: 'Schedar',        meta: 'Male' },
    { id: 'Sulafat',        label: 'Sulafat',        meta: 'Female' },
    { id: 'Umbriel',        label: 'Umbriel',        meta: 'Male' },
    { id: 'Vindemiatrix',   label: 'Vindemiatrix',   meta: 'Female' },
    { id: 'Zephyr',         label: 'Zephyr',         meta: 'Female' },
    { id: 'Zubenelgenubi',  label: 'Zubenelgenubi',  meta: 'Male' },
    { id: 'Algieba',        label: 'Algieba',        meta: 'Male' },
    { id: 'Alnilam',        label: 'Alnilam',        meta: 'Male' },
    { id: 'Autonoe',        label: 'Autonoe',        meta: 'Female' },
    { id: 'Laomedeia',      label: 'Laomedeia',      meta: 'Female' },
    { id: 'Sadaltager',     label: 'Sadaltager',     meta: 'Male' },
];

let _selectedVoice = { openai: 'nova', gemini: 'Kore' };

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function() {
    buildVoiceGrids();
    loadVoiceSettings();

    document.getElementById('tts_provider').addEventListener('change', function() {
        toggleVoiceProvider(this.value);
    });

    document.getElementById('voiceSettingsForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        document.getElementById('loading').style.display = 'block';
        try {
            const res = await saveVoiceSettings();
            if (res && res.ok) {
                showSuccess('Voice settings saved!');
            } else {
                throw new Error('Save failed');
            }
        } catch (err) {
            showError('Failed to save: ' + err.message);
        } finally {
            document.getElementById('loading').style.display = 'none';
        }
    });
});

// ── Voice ─────────────────────────────────────────────────────────────────────

function buildVoiceGrids() {
    _buildGrid('openai_voice_grid', OPENAI_VOICES, 'openai');
    _buildGrid('gemini_voice_grid', GEMINI_VOICES, 'gemini');
}

function _buildGrid(containerId, voices, provider) {
    const container = document.getElementById(containerId);
    container.innerHTML = '';
    voices.forEach(v => {
        const card = document.createElement('div');
        card.className = 'voice-card';
        card.dataset.voice = v.id;
        card.dataset.provider = provider;
        card.innerHTML = `
            <span class="vc-name">${v.label}</span>
            <span class="vc-meta">${v.meta}</span>
            <span class="vc-preview" title="Preview this voice">
                <i class="fas fa-play-circle"></i> Preview
            </span>`;
        card.addEventListener('click', (e) => {
            if (e.target.closest('.vc-preview')) return;
            _selectVoiceCard(provider, v.id);
        });
        card.querySelector('.vc-preview').addEventListener('click', (e) => {
            e.stopPropagation();
            _selectVoiceCard(provider, v.id);
            previewVoice(provider, v.id, card.querySelector('.vc-preview'));
        });
        container.appendChild(card);
    });
}

function _selectVoiceCard(provider, voiceId) {
    _selectedVoice[provider] = voiceId;
    const gridId = provider === 'openai' ? 'openai_voice_grid' : 'gemini_voice_grid';
    document.querySelectorAll(`#${gridId} .voice-card`).forEach(c => {
        c.classList.toggle('selected', c.dataset.voice === voiceId);
    });
}

function _markSelectedCards() {
    ['openai', 'gemini'].forEach(provider => {
        const sel = _selectedVoice[provider];
        const gridId = provider === 'openai' ? 'openai_voice_grid' : 'gemini_voice_grid';
        document.querySelectorAll(`#${gridId} .voice-card`).forEach(c => {
            c.classList.toggle('selected', c.dataset.voice === sel);
        });
    });
}

function toggleVoiceProvider(provider) {
    document.getElementById('openai_voice_group').style.display = provider === 'openai' ? '' : 'none';
    document.getElementById('gemini_voice_group').style.display = provider === 'gemini' ? '' : 'none';
}

async function loadVoiceSettings() {
    try {
        const res = await fetch('/api/voice/settings');
        if (!res.ok) return;
        const data = await res.json();
        if (!data.success) return;

        const provider = data.provider || 'openai';
        document.getElementById('tts_provider').value = provider;
        toggleVoiceProvider(provider);

        if (data.openai_voice) _selectedVoice.openai = data.openai_voice;
        if (data.gemini_voice) _selectedVoice.gemini = data.gemini_voice;
        if (data.gemini_model) {
            const el = document.getElementById('gemini_tts_model');
            if (el) el.value = data.gemini_model;
        }

        _markSelectedCards();

        if (!data.gemini_configured) {
            const warn = document.getElementById('gemini_api_key_warning');
            if (warn) warn.style.display = '';
        }
    } catch (err) {
        console.warn('Could not load voice settings:', err);
    }
}

async function saveVoiceSettings() {
    const provider = document.getElementById('tts_provider').value;
    const geminiModelEl = document.getElementById('gemini_tts_model');
    const payload = {
        provider,
        openai_voice: _selectedVoice.openai || 'nova',
        gemini_voice: _selectedVoice.gemini || 'Kore',
        gemini_model: geminiModelEl ? geminiModelEl.value : 'gemini-2.5-flash-preview-tts',
    };
    return fetch('/api/voice/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
}

async function previewVoice(provider, voiceId, btnEl) {
    const sampleBar = document.getElementById('voice_sample_bar');
    const player    = document.getElementById('voice_sample_player');
    const status    = document.getElementById('voice_sample_status');

    btnEl.classList.add('loading');
    btnEl.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Loading…';
    sampleBar.style.display = '';
    status.textContent = `Generating sample for ${voiceId}…`;
    player.src = '';

    try {
        const res = await fetch('/api/voice/sample', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ provider, voice: voiceId }),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.message || `HTTP ${res.status}`);
        }
        const blob = await res.blob();
        player.src = URL.createObjectURL(blob);
        player.play();
        status.textContent = `Playing: ${voiceId}`;
    } catch (err) {
        status.textContent = `Preview failed: ${err.message}`;
        console.warn('previewVoice error:', err);
    } finally {
        btnEl.classList.remove('loading');
        btnEl.innerHTML = '<i class="fas fa-play-circle"></i> Preview';
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function showError(message) {
    const el = document.getElementById('error-message');
    el.textContent = message;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 5000);
}

function showSuccess(message) {
    const el = document.getElementById('success-message');
    el.textContent = message;
    el.style.display = 'block';
    setTimeout(() => { el.style.display = 'none'; }, 3000);
}
