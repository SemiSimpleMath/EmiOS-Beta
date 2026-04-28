/**
 * Music Player - Apple MusicKit Integration
 * 
 * Handles MusicKit initialization, authorization, and playback control.
 * Communicates with backend via WebSocket for agent commands.
 */

// Global state
let musicKit = null;
let socket = null;
let isAuthorized = false;
let djModeEnabled = false;

// Frontend policy: pause/resume on AFK is a UI decision (not backend state).
const PAUSE_ON_AFK_STORAGE_KEY = 'emi_music_pause_on_afk';
let pauseOnAfkEnabled = true;
let pausedByAfk = false;
let wasPlayingBeforeAfk = false;

// Frontend policy: "DJ mode" means "auto-pick loop is enabled".
// Backend enable/disable is best-effort and may lag; the UI should not depend on it.
const DJ_MODE_STORAGE_KEY = 'emi_music_dj_mode_enabled';

function isAuthError(err) {
    const msg = (err && (err.message || err.toString())) ? String(err.message || err.toString()) : '';
    const lower = msg.toLowerCase();
    return (
        lower.includes('authorization_error') ||
        lower.includes('session has ended') ||
        lower.includes('expired token') ||
        lower.includes('forbidden') ||
        (err && err.errorCode === 'AUTHORIZATION_ERROR') ||
        (err && err.status === 401) ||
        (err && err.status === 403)
    );
}

function onUnauthorized(reason) {
    isAuthorized = false;
    updateStatus('error', reason || 'Session ended - click Authorize');

    // Show auth UI again
    if (authSection) authSection.classList.remove('hidden');
    if (playerSection) playerSection.classList.add('hidden');
    if (authorizeBtn) authorizeBtn.disabled = false;

    // Stop periodic work that assumes auth
    stopProgressReporting();

    // Clear DJ-side tracking; we can rebuild after re-auth
    djQueuedSongs = [];
    pickRequestPending = false;
}

async function forceReauthorize(reason) {
    console.warn('🎵 Authorization lost:', reason || '');
    try {
        if (musicKit && typeof musicKit.unauthorize === 'function') {
            await musicKit.unauthorize();
        }
    } catch (e) {
        // ignore
    }
    onUnauthorized(reason || 'Your session has ended. Sign in again.');
}

async function validateAuthorization() {
    if (!musicKit) return false;
    if (!musicKit.isAuthorized) return false;
    try {
        // Lightweight check: this will fail if the user session/token is no longer valid
        await musicKit.api.music('/v1/me/storefront');
        return true;
    } catch (err) {
        if (isAuthError(err)) {
            await forceReauthorize('Your session has ended. Sign in again.');
        }
        return false;
    }
}

// DOM Elements
const statusIndicator = document.getElementById('status-indicator');
const statusDot = statusIndicator?.querySelector('.status-dot');
const statusText = statusIndicator?.querySelector('.status-text');
const authSection = document.getElementById('auth-section');
const playerSection = document.getElementById('player-section');
const errorSection = document.getElementById('error-section');
const authorizeBtn = document.getElementById('authorize-btn');

// Player elements
const albumArt = document.getElementById('album-art');
const trackTitle = document.getElementById('track-title');
const trackArtist = document.getElementById('track-artist');
const trackAlbum = document.getElementById('track-album');
const timeCurrent = document.getElementById('time-current');
const timeTotal = document.getElementById('time-total');
const progressBar = document.getElementById('progress-bar');
const progressFill = document.getElementById('progress-fill');
const playPauseIcon = document.getElementById('play-pause-icon');
const volumeSlider = document.getElementById('volume-slider');
const searchInput = document.getElementById('search-input');
const searchResults = document.getElementById('search-results');

// Song meta UI elements
const songMetaCard = document.getElementById('song-meta-card');
const songMetaGrid = document.getElementById('song-meta-grid');
const songMetaSub = document.getElementById('song-meta-sub');
const btnDecrTrack = document.getElementById('btn-decr-track');
const btnDecrArtist = document.getElementById('btn-decr-artist');
const btnDecrGenre = document.getElementById('btn-decr-genre');
const btnIncrTrack = document.getElementById('btn-incr-track');
const btnIncrArtist = document.getElementById('btn-incr-artist');
const btnIncrGenre = document.getElementById('btn-incr-genre');
const btnBanTrack = document.getElementById('btn-ban-track');
const btnBanArtist = document.getElementById('btn-ban-artist');
const btnBanGenre = document.getElementById('btn-ban-genre');

// DJ UI elements
const djThinkingEl = document.getElementById('dj-thinking');

let lastSongMeta = null;
let lastNowPlayingForMeta = { title: '', artist: '' };
const MIN_WEIGHT_FACTOR = 0.05;
const WEIGHT_ADJUST_DEBOUNCE_MS = 600;
const WEIGHT_STEP = 0.1;

// Debounced set: key -> { payload, timer }
// We treat the UI value as authoritative and send set_factor after debounce.
const pendingWeightSet = new Map();

function _weightKey(scope, meta) {
    if (!meta) return '';
    if (scope === 'track') {
        const tid = (meta.track_id || meta.trackId || '').toString().trim();
        if (tid) return `track_id:${tid}`;
        return `track:${(meta.title || '').trim()}|||${(meta.artist || '').trim()}`;
    }
    if (scope === 'artist') return `artist:${(meta.artist || '').trim()}`;
    if (scope === 'genre') return `genre:${(meta.genre || '').trim()}`;
    return '';
}

function _getCurrentScopeWeight(scope, meta) {
    const weights = meta?.weights || meta?.override_factors || {};
    const v = Number(weights?.[scope]);
    return isFinite(v) ? v : 1.0;
}

function _setCurrentScopeWeight(scope, meta, value) {
    if (!meta) return;
    if (!meta.weights) meta.weights = {};
    meta.weights[scope] = value;
}

function _recomputeEffectiveWeight(meta) {
    if (!meta) return;
    const rowW = Number(meta.row_weight ?? meta.base_prob_factor ?? 1.0);
    const wTrack = _getCurrentScopeWeight('track', meta);
    const wArtist = _getCurrentScopeWeight('artist', meta);
    const wGenre = _getCurrentScopeWeight('genre', meta);
    const eff = Math.max(0, rowW) * Math.max(0, wTrack) * Math.max(0, wArtist) * Math.max(0, wGenre);
    meta.effective_weight = eff;
}

async function flushWeightAdjust(key) {
    const entry = pendingWeightSet.get(key);
    if (!entry) return;
    pendingWeightSet.delete(key);
    if (entry.timer) clearTimeout(entry.timer);

    try {
        const resp = await fetch('/api/music/weights/adjust', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(entry.payload),
        });
        const data = await resp.json();
        if (data && data.status === 'ok' && lastSongMeta && lastSongMeta.found) {
            // Apply authoritative value from backend
            _setCurrentScopeWeight(entry.payload.scope, lastSongMeta, Number(data.new_factor));
            _recomputeEffectiveWeight(lastSongMeta);
            renderSongMeta(lastSongMeta);
        }
    } catch (err) {
        console.warn('Failed to flush weight adjust:', err);
    }
}

function enqueueWeightSet(scope, delta) {
    if (!lastSongMeta || !lastSongMeta.found) return;
    const key = _weightKey(scope, lastSongMeta);
    if (!key) return;

    // Optimistic update in UI (clamp decrements to floor)
    const cur = _getCurrentScopeWeight(scope, lastSongMeta);
    let next = cur + delta;
    if (delta < 0) next = Math.max(MIN_WEIGHT_FACTOR, next);
    else next = Math.max(0.0, next);

    _setCurrentScopeWeight(scope, lastSongMeta, next);
    _recomputeEffectiveWeight(lastSongMeta);
    renderSongMeta(lastSongMeta);

    // Build/replace pending payload with authoritative final value
    const basePayload = { scope: scope, set_factor: next };
    if (scope === 'track') {
        const tid = (lastSongMeta.track_id || lastSongMeta.trackId || '').toString().trim();
        if (tid) basePayload.track_id = tid;
        basePayload.title = lastSongMeta.title;
        basePayload.artist = lastSongMeta.artist;
    } else if (scope === 'artist') {
        basePayload.artist = lastSongMeta.artist;
    } else if (scope === 'genre') {
        basePayload.genre = lastSongMeta.genre;
    }

    const existing = pendingWeightSet.get(key);
    const merged = existing ? existing : { payload: basePayload, timer: null };
    merged.payload = basePayload;
    if (merged.timer) clearTimeout(merged.timer);
    merged.timer = setTimeout(() => flushWeightAdjust(key), WEIGHT_ADJUST_DEBOUNCE_MS);
    pendingWeightSet.set(key, merged);
}

function _fmtNum(x, digits = 1) {
    const n = Number(x);
    if (!isFinite(n)) return '--';
    return n.toFixed(digits);
}

function renderSongMeta(meta) {
    lastSongMeta = meta || null;
    if (!songMetaCard || !songMetaGrid) return;

    if (!meta) {
        songMetaCard.classList.add('hidden');
        return;
    }

    // Always show the card when we have any response (found or not),
    // so the UI explains why stats are missing.
    songMetaCard.classList.remove('hidden');
    if (songMetaSub) {
        if (meta.found) {
            const g = meta.genre ? `genre: ${meta.genre}` : 'genre: --';
            const w = `w: ${_fmtNum(meta.effective_weight ?? meta.prob_factor, 3)}`;
            const ms = meta.match_strategy ? `match: ${meta.match_strategy}` : '';
            songMetaSub.textContent = [g, w, ms].filter(Boolean).join(' | ');
        } else {
            const t = (meta.title || '').trim();
            const a = (meta.artist || '').trim();
            songMetaSub.textContent = `no dataset match${t || a ? ` (${t || '--'} — ${a || '--'})` : ''}`;
        }
    }

    if (!meta.found) {
        // For this UI, "no match" simply means "no DJ-provided dataset payload".
        // Hide the card to avoid misleading messages.
        songMetaCard.classList.add('hidden');
        return;
    }

    const s = meta.sliders || {};
    const items = [
        ['Energy', s.energy],
        ['Valence', s.valence],
        ['Loud', s.loudness],
        ['Speech', s.speechiness],
        ['Acoustic', s.acousticness],
        ['Instr', s.instrumentalness],
        ['Live', s.liveness],
        ['Tempo', s.tempo],
        ['Row w', meta.row_weight ?? meta.base_prob_factor],
        ['W track', meta.weights?.track ?? meta.override_factors?.track],
        ['W artist', meta.weights?.artist ?? meta.override_factors?.artist],
        ['W genre', meta.weights?.genre ?? meta.override_factors?.genre],
    ];

    songMetaGrid.innerHTML = items.map(([k, v]) => {
        return `
            <div class="song-meta-item">
                <div class="song-meta-k">${k}</div>
                <div class="song-meta-v">${_fmtNum(v, k.includes('pf') || k.startsWith('Ov') ? 3 : 1)}</div>
            </div>
        `;
    }).join('');

    // Enable/disable controls based on what we have
    if (btnDecrTrack) btnDecrTrack.disabled = !(meta.title && meta.artist);
    if (btnDecrArtist) btnDecrArtist.disabled = !(meta.artist);
    if (btnDecrGenre) btnDecrGenre.disabled = !(meta.genre);
    if (btnIncrTrack) btnIncrTrack.disabled = !(meta.title && meta.artist);
    if (btnIncrArtist) btnIncrArtist.disabled = !(meta.artist);
    if (btnIncrGenre) btnIncrGenre.disabled = !(meta.genre);
    if (btnBanTrack) btnBanTrack.disabled = !(meta.title && meta.artist);
    if (btnBanArtist) btnBanArtist.disabled = !(meta.artist);
    if (btnBanGenre) btnBanGenre.disabled = !(meta.genre);

    // Optional: once at/below floor, disable further decrements (but still allow Ban).
    try {
        const wTrack = Number(meta.weights?.track ?? meta.override_factors?.track);
        const wArtist = Number(meta.weights?.artist ?? meta.override_factors?.artist);
        const wGenre = Number(meta.weights?.genre ?? meta.override_factors?.genre);
        if (btnDecrTrack && isFinite(wTrack) && wTrack <= MIN_WEIGHT_FACTOR + 1e-9) btnDecrTrack.disabled = true;
        if (btnDecrArtist && isFinite(wArtist) && wArtist <= MIN_WEIGHT_FACTOR + 1e-9) btnDecrArtist.disabled = true;
        if (btnDecrGenre && isFinite(wGenre) && wGenre <= MIN_WEIGHT_FACTOR + 1e-9) btnDecrGenre.disabled = true;
    } catch (e) {
        // ignore
    }
}

// NOTE: No GET lookups for stats. Stats are provided in the DJ payload.

function renderSongMetaFromDatasetPayload(ds) {
    if (!ds || typeof ds !== 'object') {
        return;
    }
    const t = (ds.title || '').toString().trim();
    const a = (ds.artist || '').toString().trim();
    const trackId = (ds.track_id || ds.trackId || '').toString().trim();
    const sliders = (ds.sliders && typeof ds.sliders === 'object') ? ds.sliders : {};
    const genre = (ds.genre || '').toString().trim() || null;
    const rowW = (ds.prob_factor !== undefined && ds.prob_factor !== null) ? Number(ds.prob_factor) : 1.0;
    const weights = (ds.weights && typeof ds.weights === 'object') ? ds.weights : {};
    const wTrack = isFinite(Number(weights.track)) ? Number(weights.track) : 1.0;
    const wArtist = isFinite(Number(weights.artist)) ? Number(weights.artist) : 1.0;
    const wGenre = isFinite(Number(weights.genre)) ? Number(weights.genre) : 1.0;
    renderSongMeta({
        found: true,
        track_id: trackId || null,
        title: t,
        artist: a,
        genre: genre,
        sliders: sliders,
        match_strategy: 'dataset_payload',
        match_score: null,
        row_weight: isFinite(rowW) ? rowW : 1.0,
        effective_weight: isFinite(rowW) ? rowW : 1.0,
        weights: { track: wTrack, artist: wArtist, genre: wGenre },
    });
}

async function decrementWeight(scope) {
    enqueueWeightSet(scope, -WEIGHT_STEP);
}

async function incrementWeight(scope) {
    enqueueWeightSet(scope, WEIGHT_STEP);
}

async function banWeight(scope) {
    if (!lastSongMeta || !lastSongMeta.found) return;
    const body = { scope: scope, set_factor: 0.0 };
    if (scope === 'track') {
        body.title = lastSongMeta.title;
        body.artist = lastSongMeta.artist;
    } else if (scope === 'artist') {
        body.artist = lastSongMeta.artist;
    } else if (scope === 'genre') {
        body.genre = lastSongMeta.genre;
    }

    try {
        // Flush any pending adjustment for this key before banning.
        const key = _weightKey(scope, lastSongMeta);
        if (key && pendingWeightSet.has(key)) {
            await flushWeightAdjust(key);
        }

        const resp = await fetch('/api/music/weights/adjust', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        if (data && data.status === 'ok') {
            // Apply immediately
            _setCurrentScopeWeight(scope, lastSongMeta, 0.0);
            _recomputeEffectiveWeight(lastSongMeta);
            renderSongMeta(lastSongMeta);
        }
    } catch (err) {
        console.warn('Failed to ban weight:', err);
    }
}

/**
 * Update status indicator
 */
function updateStatus(status, text) {
    if (!statusDot || !statusText) return;
    
    statusDot.className = 'status-dot';
    if (status) {
        statusDot.classList.add(status);
    }
    statusText.textContent = text;
}

/**
 * Show error state
 */
function showError(message) {
    if (authSection) authSection.classList.add('hidden');
    if (playerSection) playerSection.classList.add('hidden');
    if (errorSection) {
        errorSection.classList.remove('hidden');
        const errorMsg = document.getElementById('error-message');
        if (errorMsg) errorMsg.textContent = message;
    }
    updateStatus('error', 'Error');
}

/**
 * Format time in M:SS format
 */
function formatTime(seconds) {
    if (!seconds || isNaN(seconds)) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

/**
 * Initialize WebSocket connection
 */
function initSocket() {
    // Use default connection settings (current origin). Avoid constructing
    // a URL with an empty port (e.g. "http://host:"), which breaks sockets.
    socket = io();
    
    if (window.EmiSocketHeartbeat) {
        window.EmiSocketHeartbeat.install({ socket, getRoomId: () => "music" });
    }

    socket.on('connect', () => {
        console.log('🔌 Socket connected');

        // Register this tab as the MUSIC client (separate from chat tab)
        socket.emit('register_music_client', { room_id: "music" });
        console.log('🎵 Registered as music client');

        // If DJ mode is enabled, kick a queue check as soon as we have a live socket.
        try {
            setTimeout(() => checkQueueNeedsSongs(), 50);
        } catch (e) {
            // ignore
        }
    });

    socket.on('connect_error', (err) => {
        console.warn('🔌 Socket connect_error:', err);
        updateStatus('error', 'Socket connection failed');
    });

    socket.on('disconnect', (reason) => {
        console.warn('🔌 Socket disconnected:', reason);
        updateStatus('error', 'Socket disconnected');
    });
    
    // Listen for agent music commands
    socket.on('music_command', (data) => {
        console.log('🎵 Received music command:', data);
        handleMusicCommand(data);
    });

    // AFK state relay from backend (frontend decides policy)
    socket.on('music_afk_state', (data) => {
        try {
            handleMusicAfkState(data);
        } catch (e) {
            console.warn('music_afk_state handler failed:', e);
        }
    });

    // Explicit wake signal from backend AFK manager.
    socket.on('music_wake_signal', (data) => {
        try {
            handleMusicWakeSignal(data);
        } catch (e) {
            console.warn('music_wake_signal handler failed:', e);
        }
    });
}

/**
 * Handle music commands from agent/backend
 * 
 * IMPORTANT: queue_next commands are deduplicated by query to prevent
 * duplicate recordings from socket reconnection/retry issues.
 */
let lastQueueCommand = { query: null, timestamp: 0 };

function loadPauseOnAfkSetting() {
    try {
        const raw = localStorage.getItem(PAUSE_ON_AFK_STORAGE_KEY);
        if (raw === null) return;
        pauseOnAfkEnabled = raw === '1' || raw === 'true';
    } catch (e) {
        // ignore
    }
}

function savePauseOnAfkSetting(enabled) {
    try {
        localStorage.setItem(PAUSE_ON_AFK_STORAGE_KEY, enabled ? '1' : '0');
    } catch (e) {
        // ignore
    }
}

async function handleMusicAfkState(data) {
    if (!musicKit || !isAuthorized) return;
    if (!pauseOnAfkEnabled) return;

    const isAfk = !!(data && data.is_afk);
    const justWentAfk = !!(data && data.just_went_afk);
    const justReturned = !!(data && data.just_returned);

    if (justWentAfk && isAfk) {
        // Only pause if we were actively playing.
        wasPlayingBeforeAfk = !!musicKit.isPlaying;
        if (wasPlayingBeforeAfk) {
            try {
                await musicKit.pause();
                pausedByAfk = true;
            } catch (err) {
                console.warn('AFK pause failed:', err);
            }
        }
        return;
    }

    if (justReturned && !isAfk) {
        await resumeFromAfkIfNeeded('music_afk_state');
    }
}

async function resumeFromAfkIfNeeded(source = 'unknown') {
    // Only resume if we paused due to AFK and were playing before AFK.
    if (pausedByAfk && wasPlayingBeforeAfk) {
        try {
            await musicKit.play();
            console.log(`🎵 Resumed playback on wake (${source})`);
        } catch (err) {
            console.warn('AFK resume failed:', err);
        }
    }
    pausedByAfk = false;
    wasPlayingBeforeAfk = false;
}

async function handleMusicWakeSignal(_data) {
    if (!musicKit || !isAuthorized) return;
    await resumeFromAfkIfNeeded('music_wake_signal');
}

async function handleMusicCommand(data) {
    if (!musicKit || !isAuthorized) {
        console.warn('MusicKit not ready for commands');
        return;
    }
    
    const { command, payload } = data;
    
    try {
        switch (command) {
            case 'play':
                await musicKit.play();
                break;
            case 'pause':
                await musicKit.pause();
                break;
            case 'next':
                await musicKit.skipToNextItem();
                break;
            case 'previous':
                await musicKit.skipToPreviousItem();
                break;
            case 'set_volume':
                if (payload && payload.volume !== undefined) {
                    musicKit.volume = payload.volume;
                }
                break;
            case 'search_and_play':
                if (payload && payload.query) {
                    await searchAndPlay(payload.query);
                }
                break;
            case 'queue_next':
                if (payload && (payload.query || payload.candidates)) {
                    // Dedupe: Ignore duplicate queue_next for same query within 30 seconds
                    const now = Date.now();
                    const key = payload.query || JSON.stringify((payload.candidates || []).map(c => c?.track_id || `${c?.title || ''}|||${c?.artist || ''}`));
                    if (key === lastQueueCommand.query && 
                        (now - lastQueueCommand.timestamp) < 30000) {
                        console.warn(`🎵 Ignoring duplicate queue_next (${(now - lastQueueCommand.timestamp)/1000}s since last)`);
                        return;
                    }
                    lastQueueCommand = { query: key, timestamp: now };

                    if (Array.isArray(payload.candidates) && payload.candidates.length > 0) {
                        await queueNextSongFromCandidates(payload.candidates);
                    } else if (payload.query) {
                        await queueNextSong(payload.query, payload.dataset || null);
                    }
                }
                break;
            case 'get_state':
                emitPlaybackState();
                break;
        }
    } catch (err) {
        console.error('Error handling music command:', err);
    }
}

// Progress reporting interval
let progressReportInterval = null;

/**
 * Emit current playback state to backend
 */
function emitPlaybackState() {
    if (!socket || !musicKit) return;
    
    const nowPlaying = musicKit.nowPlayingItem;
    const progress = musicKit.currentPlaybackTime || 0;
    const duration = musicKit.currentPlaybackDuration || 0;
    const remaining = duration > 0 ? duration - progress : 0;
    
    const state = {
        is_playing: musicKit.isPlaying,
        current_track: nowPlaying ? {
            title: nowPlaying.title,
            artist: nowPlaying.artistName,
            album: nowPlaying.albumName,
            duration: nowPlaying.playbackDuration / 1000,  // Convert ms to seconds
            artwork_url: nowPlaying.artwork?.url?.replace('{w}', '300').replace('{h}', '300'),
        } : null,
        progress_seconds: progress,
        duration_seconds: duration,
        time_remaining_seconds: remaining,
        volume: musicKit.volume,
    };
    
    socket.emit('music_state_update', state);
}

/**
 * Start periodic progress reporting (for continuous DJ mode)
 */
function startProgressReporting() {
    if (progressReportInterval) return;
    
    progressReportInterval = setInterval(() => {
        if (musicKit) {
            // Always check queue, even if paused (might need to buffer)
            checkQueueNeedsSongs();
            
            if (musicKit.isPlaying) {
                emitPlaybackState();
            }
        }
    }, 5000); // Check every 5 seconds
    
    console.log('🎵 Started progress reporting');
}

/**
 * Stop periodic progress reporting
 */
function stopProgressReporting() {
    if (progressReportInterval) {
        clearInterval(progressReportInterval);
        progressReportInterval = null;
        console.log('🎵 Stopped progress reporting');
    }
}

/**
 * Fetch developer token from backend
 */
async function fetchDeveloperToken() {
    try {
        const origin = window.location.origin;
        const response = await fetch(`/api/music/token?origin=${encodeURIComponent(origin)}`);
        
        if (!response.ok) {
            const error = await response.json();
            throw new Error(error.message || 'Failed to get token');
        }
        
        const data = await response.json();
        return data.developer_token;
    } catch (err) {
        console.error('Error fetching developer token:', err);
        throw err;
    }
}

/**
 * Initialize MusicKit
 */
async function initMusicKit() {
    updateStatus('', 'Fetching token...');
    
    try {
        // Get developer token from backend
        const developerToken = await fetchDeveloperToken();
        
        if (!developerToken) {
            throw new Error('No developer token received');
        }
        
        updateStatus('', 'Loading MusicKit...');
        
        // Wait for MusicKit to be available
        await new Promise((resolve, reject) => {
            if (window.MusicKit) {
                resolve();
                return;
            }
            
            // Wait up to 10 seconds for MusicKit to load
            let attempts = 0;
            const check = setInterval(() => {
                attempts++;
                if (window.MusicKit) {
                    clearInterval(check);
                    resolve();
                } else if (attempts > 100) {
                    clearInterval(check);
                    reject(new Error('MusicKit failed to load'));
                }
            }, 100);
        });
        
        updateStatus('', 'Configuring...');
        
        // Configure MusicKit
        await MusicKit.configure({
            developerToken: developerToken,
            app: {
                name: 'Emi Music',
                build: '1.0.0',
            },
        });
        
        musicKit = MusicKit.getInstance();
        
        // Set up event listeners
        setupMusicKitListeners();
        
        // Check if user is already authorized (token persisted from previous session)
        if (musicKit.isAuthorized) {
            console.log('✅ MusicKit initialized - already authorized from previous session');
            const ok = await validateAuthorization();
            if (ok) {
                onAuthorized();
            } else {
                console.warn('🎵 Persisted authorization was invalid - reauth required');
            }
        } else {
            updateStatus('connected', 'Ready - Click Authorize');
            
            // Enable authorize button
            if (authorizeBtn) {
                authorizeBtn.disabled = false;
            }
            
            console.log('✅ MusicKit initialized - waiting for authorization');
        }
        
    } catch (err) {
        console.error('Failed to initialize MusicKit:', err);
        showError(err.message || 'Failed to initialize MusicKit');
    }
}

/**
 * Set up MusicKit event listeners
 */
function setupMusicKitListeners() {
    if (!musicKit) return;
    
    // Playback state changes
    musicKit.addEventListener('playbackStateDidChange', () => {
        updatePlayerUI();
        emitPlaybackState();
        
        // Check if DJ needs to queue more songs
        checkQueueNeedsSongs();
    });
    
    // Now playing item changes
    musicKit.addEventListener('nowPlayingItemDidChange', () => {
        updateNowPlaying();
        emitPlaybackState();
        
        // Check if a DJ-queued song started playing, remove from tracking
        const nowPlaying = musicKit.nowPlayingItem;
        let fetchedMeta = false;
        if (nowPlaying && djQueuedSongs.length > 0) {
            const nowId = nowPlaying?.id || null;
            const idx = djQueuedSongs.findIndex(s => {
                if (nowId && s?.id) return String(s.id) === String(nowId);
                return s.title === nowPlaying.title && s.artist === nowPlaying.artistName;
            });
            if (idx !== -1) {
                const removed = djQueuedSongs.splice(idx, 1)[0];
                console.log(`🎵 DJ-queued song started playing: "${removed.title}", ${djQueuedSongs.length} remaining`);

                // If DJ provided dataset payload, render stats directly from it.
                try {
                    const ds = removed?.dataset;
                    if (ds && typeof ds === 'object' && ds.sliders) {
                        renderSongMetaFromDatasetPayload(ds);
                        fetchedMeta = true;
                    }
                } catch (e) {
                    // ignore
                }
            }
        }

        // If this wasn't a DJ-queued song, do not show stats (no lookups).
        if (nowPlaying && !fetchedMeta) renderSongMeta(null);
    });
    
    // Playback time updates
    musicKit.addEventListener('playbackTimeDidChange', () => {
        updateProgress();
    });
    
    // Authorization changes
    musicKit.addEventListener('authorizationStatusDidChange', () => {
        if (musicKit.isAuthorized) {
            onAuthorized();
        } else {
            onUnauthorized('Not authorized - click Authorize');
        }
    });
}

/**
 * Check if DJ should request more songs based on queue state
 */
// How many seconds before song end to request next song
// Needs to be high enough for LLM to generate 5-10 candidates (~20-30s)
const DJ_QUEUE_THRESHOLD_SECONDS = 90;

function checkQueueNeedsSongs() {
    if (!djModeEnabled) return;
    if (!musicKit) return;
    
    // Use our own tracking instead of MusicKit's queue (which seems unreliable)
    const queueLen = getQueueLength();
    let djPending = djQueuedSongs.length;
    const timeRemaining = musicKit.currentPlaybackDuration 
        ? musicKit.currentPlaybackDuration - musicKit.currentPlaybackTime 
        : 0;
    const hasNowPlaying = !!musicKit.nowPlayingItem;

    // If playback is stopped and the MusicKit queue is empty, our DJ-tracking can
    // get stale (e.g., user skips over a queued item). In that case, prefer reality.
    const definitelyEmpty = (!hasNowPlaying && queueLen === 0);
    if (definitelyEmpty && djPending > 0) {
        console.warn(`🎵 Queue empty but DJ tracking had ${djPending} pending. Clearing stale tracking.`);
        djQueuedSongs = [];
        djPending = 0;
    }
    
    let shouldRequest = false;
    let reason = '';
    
    if (definitelyEmpty && djPending === 0) {
        // Nothing playing and nothing queued - need song immediately
        shouldRequest = true;
        reason = 'queue empty (nothing playing)';
    } else if (!hasNowPlaying && djPending === 0) {
        // Nothing playing, nothing queued - need song immediately
        shouldRequest = true;
        reason = 'nothing playing, no DJ songs pending';
    } else if (djPending === 0 && (queueLen === 0 || timeRemaining < DJ_QUEUE_THRESHOLD_SECONDS)) {
        // Playing but no DJ songs pending and song ending soon
        shouldRequest = true;
        reason = `no DJ songs pending, queueLen=${queueLen}, ${timeRemaining.toFixed(0)}s remaining`;
    } else if (djPending === 1 && timeRemaining < DJ_QUEUE_THRESHOLD_SECONDS) {
        // One DJ song pending but current ending soon - buffer another
        shouldRequest = true;
        reason = `1 DJ song pending, ${timeRemaining.toFixed(0)}s remaining`;
    }
    
    if (shouldRequest) {
        console.log(`🎵 Need song: ${reason}`);
        logQueueContents();  // Debug: show MusicKit queue state
        requestDJPick();
    }
}

/**
 * Get the number of songs in the queue (after current song)
 */
function getQueueLength() {
    if (!musicKit || !musicKit.queue) return 0;
    
    const items = musicKit.queue.items || [];
    const position = musicKit.queue.position || 0;
    
    // Queue length = total items - current position - 1 (for current song)
    const remaining = Math.max(0, items.length - position - 1);
    
    return remaining;
}

/**
 * Debug: Log full queue contents
 */
function logQueueContents() {
    if (!musicKit || !musicKit.queue) {
        console.log('🎵 Queue: not available');
        return;
    }
    
    const items = musicKit.queue.items || [];
    const position = musicKit.queue.position;
    const nowPlaying = musicKit.nowPlayingItem;
    
    console.log(`🎵 Queue debug: total=${items.length}, position=${position}`);
    console.log(`🎵 Now playing: ${nowPlaying?.title || 'none'} by ${nowPlaying?.artistName || '?'}`);
    
    if (items.length > 0) {
        console.log('🎵 Queue contents:');
        items.forEach((item, idx) => {
            const marker = idx === position ? '→ ' : '  ';
            const status = idx < position ? '(played)' : idx === position ? '(current)' : '(upcoming)';
            console.log(`${marker}[${idx}] ${item.title} by ${item.artistName} ${status}`);
        });
    }
}

/**
 * Request DJ to pick a new song
 */
let pickRequestPending = false;

// Track songs WE queued that haven't played yet
let djQueuedSongs = [];

function setDjThinking(isThinking, text) {
    if (!djThinkingEl) return;
    if (isThinking) {
        djThinkingEl.classList.remove('hidden');
        djThinkingEl.textContent = text || 'Thinking...';
    } else {
        djThinkingEl.classList.add('hidden');
        djThinkingEl.textContent = 'Thinking...';
    }
}

function requestDJPick() {
    if (pickRequestPending) {
        console.log('🎵 Pick request already pending, skipping');
        return;
    }
    
    // Check our own tracking - if we've queued songs that haven't played, don't request more
    if (djQueuedSongs.length >= 2) {
        console.log(`🎵 Already have ${djQueuedSongs.length} DJ-queued songs pending, skipping request`);
        return;
    }
    
    if (!socket || socket.disconnected) {
        console.warn('🎵 Cannot request DJ pick: socket not connected');
        setDjThinking(false);
        return;
    }
    
    pickRequestPending = true;
    setDjThinking(true, 'Thinking...');
    
    socket.emit('music_pick_request', {
        queue_length: getQueueLength(),
        dj_queued_count: djQueuedSongs.length,
        timestamp: new Date().toISOString()
    });
    
    // Reset pending flag after timeout (in case response never comes)
    // Timeout needs to be high enough for LLM to generate 5-10 candidates
    setTimeout(() => {
        pickRequestPending = false;
        setDjThinking(false);
    }, 60000);
}

/**
 * Handle authorization
 */
async function authorize() {
    if (!musicKit) {
        console.error('MusicKit not initialized');
        return;
    }
    
    try {
        updateStatus('', 'Authorizing...');
        
        // This will show Apple's sign-in popup
        const musicUserToken = await musicKit.authorize();
        
        console.log('✅ Got Music User Token');
        onAuthorized();
        
    } catch (err) {
        console.error('Authorization failed:', err);
        if (isAuthError(err)) {
            await forceReauthorize('Your session has ended. Sign in again.');
        } else {
            updateStatus('error', 'Auth failed');
        }
    }
}

/**
 * Called when user is authorized
 */
function onAuthorized() {
    isAuthorized = true;
    updateStatus('authorized', 'Connected');
    
    // Show player, hide auth
    if (authSection) authSection.classList.add('hidden');
    if (playerSection) playerSection.classList.remove('hidden');
    
    console.log('🎵 Apple Music authorized - ready to play!');
}

/**
 * Update player UI based on playback state
 */
function updatePlayerUI() {
    if (!musicKit || !playPauseIcon) return;
    
    if (musicKit.isPlaying) {
        playPauseIcon.className = 'fas fa-pause';
    } else {
        playPauseIcon.className = 'fas fa-play';
    }
}

/**
 * Update now playing display
 */
function updateNowPlaying() {
    if (!musicKit) return;
    
    const nowPlaying = musicKit.nowPlayingItem;
    
    if (nowPlaying) {
        if (trackTitle) trackTitle.textContent = nowPlaying.title || 'Unknown Track';
        if (trackArtist) trackArtist.textContent = nowPlaying.artistName || 'Unknown Artist';
        if (trackAlbum) trackAlbum.textContent = nowPlaying.albumName || '';
        
        // Update album art
        if (albumArt && nowPlaying.artwork) {
            const artworkUrl = nowPlaying.artwork.url?.replace('{w}', '600').replace('{h}', '600');
            if (artworkUrl) {
                albumArt.innerHTML = `<img src="${artworkUrl}" alt="Album Art">`;
            }
        }
        
        // Update total time
        // playbackDuration from media item is in milliseconds, convert to seconds
        if (timeTotal) {
            const durationSec = nowPlaying.playbackDuration / 1000;
            timeTotal.textContent = formatTime(durationSec);
        }

        // Song meta fetching is handled in nowPlayingItemDidChange so we can prefer
        // canonical dataset title/artist when the DJ queued the song.
    } else {
        if (trackTitle) trackTitle.textContent = 'No Track Playing';
        if (trackArtist) trackArtist.textContent = '--';
        if (trackAlbum) trackAlbum.textContent = '--';
        if (albumArt) albumArt.innerHTML = '<i class="fas fa-music placeholder-icon"></i>';
        if (timeTotal) timeTotal.textContent = '0:00';
        renderSongMeta(null);
    }
}

/**
 * Update progress bar
 */
function updateProgress() {
    if (!musicKit) return;
    
    const current = musicKit.currentPlaybackTime;
    const duration = musicKit.currentPlaybackDuration;
    
    if (timeCurrent) {
        timeCurrent.textContent = formatTime(current);
    }
    
    if (progressFill && duration > 0) {
        const percent = (current / duration) * 100;
        progressFill.style.width = `${percent}%`;
    }
}

// =============================================================================
// Fetch Music Taste
// =============================================================================

/**
 * Fetch user's recently played and library to generate taste profile
 */
async function fetchMusicTaste() {
    if (!musicKit || !isAuthorized) {
        alert('Please authorize Apple Music first');
        return;
    }
    
    const btn = document.getElementById('fetch-taste-btn');
    const resultsDiv = document.getElementById('taste-results');
    const output = document.getElementById('taste-output');
    
    if (btn) {
        btn.disabled = true;
        btn.querySelector('span').textContent = 'Fetching...';
    }
    
    let tasteText = '## My Music Taste (from Apple Music)\n\n';
    
    try {
        // Try different API methods - MusicKit v3 has different ways to access data
        
        // Method 1: Recently played via library API
        try {
            const recentlyPlayed = await musicKit.api.music('/v1/me/recent/played/tracks', {
                limit: 25
            });
            
            tasteText += '### Recently Played Tracks\n';
            if (recentlyPlayed.data?.data?.length > 0) {
                for (const item of recentlyPlayed.data.data) {
                    const attrs = item.attributes || {};
                    tasteText += `- ${attrs.name} by ${attrs.artistName}`;
                    if (attrs.genreNames?.length > 0) {
                        tasteText += ` [${attrs.genreNames[0]}]`;
                    }
                    tasteText += '\n';
                }
            } else {
                tasteText += '(No recently played data)\n';
            }
        } catch (e) {
            console.log('Recently played tracks not available:', e.message);
            tasteText += '### Recently Played\n(Not available via API)\n';
        }
        
        // Method 2: User's library songs (what they've added)
        try {
            const librarySongs = await musicKit.api.music('/v1/me/library/songs', {
                limit: 30
            });
            
            tasteText += '\n### Songs in My Library\n';
            if (librarySongs.data?.data?.length > 0) {
                // Group by artist for cleaner output
                const byArtist = {};
                for (const item of librarySongs.data.data) {
                    const attrs = item.attributes || {};
                    const artist = attrs.artistName || 'Unknown';
                    if (!byArtist[artist]) byArtist[artist] = [];
                    byArtist[artist].push(attrs.name);
                }
                
                for (const [artist, songs] of Object.entries(byArtist)) {
                    tasteText += `- ${artist}: ${songs.slice(0, 3).join(', ')}`;
                    if (songs.length > 3) tasteText += ` (+${songs.length - 3} more)`;
                    tasteText += '\n';
                }
            } else {
                tasteText += '(No library songs)\n';
            }
        } catch (e) {
            console.log('Library songs not available:', e.message);
            tasteText += '\n### Library Songs\n(Not available via API)\n';
        }
        
        // Method 3: User's playlists
        try {
            const playlists = await musicKit.api.music('/v1/me/library/playlists', {
                limit: 10
            });
            
            tasteText += '\n### My Playlists\n';
            if (playlists.data?.data?.length > 0) {
                for (const item of playlists.data.data) {
                    const attrs = item.attributes || {};
                    tasteText += `- ${attrs.name}\n`;
                }
            } else {
                tasteText += '(No playlists)\n';
            }
        } catch (e) {
            console.log('Playlists not available:', e.message);
        }
        
        tasteText += '\n---\nPaste relevant parts into resources/resource_music_preferences.md';
        
    } catch (err) {
        console.error('Error fetching music taste:', err);
        tasteText += `\nError: ${err.message}`;
    }
    
    if (output) output.textContent = tasteText;
    if (resultsDiv) resultsDiv.classList.remove('hidden');
    
    if (btn) {
        btn.disabled = false;
        btn.querySelector('span').textContent = 'Fetch My Music Taste';
    }
}

/**
 * Copy taste output to clipboard
 */
function copyTasteToClipboard() {
    const output = document.getElementById('taste-output');
    if (!output) return;
    
    navigator.clipboard.writeText(output.textContent).then(() => {
        const btn = document.getElementById('copy-taste-btn');
        if (btn) {
            const originalText = btn.innerHTML;
            btn.innerHTML = '<i class="fas fa-check"></i> Copied!';
            setTimeout(() => btn.innerHTML = originalText, 2000);
        }
    });
}

/**
 * Search for songs
 */
async function searchSongs(query) {
    if (!musicKit || !isAuthorized || !query) return [];
    
    try {
        const results = await musicKit.api.music(`/v1/catalog/us/search`, {
            term: query,
            types: 'songs',
            limit: 10,
        });
        
        return results.data.results.songs?.data || [];
    } catch (err) {
        console.error('Search failed:', err);
        if (isAuthError(err)) {
            await forceReauthorize('Your session has ended. Sign in again.');
        }
        return [];
    }
}

/**
 * Search and play first result
 */
async function searchAndPlay(query) {
    const results = await searchSongs(query);
    if (results.length > 0) {
        await playSong(results[0]);
    }
}

/**
 * Search and queue song to play next (for continuous mode)
 */
async function queueNextSong(query, datasetMeta = null) {
    if (!musicKit || !isAuthorized) {
        console.warn('MusicKit not ready for queueing');
        return false;
    }
    
    try {
        const results = await searchSongs(query);
        if (results.length === 0) {
            console.warn('No results for queue query:', query);
            
            return false;
        }
        
        // If DJ provided canonical dataset title/artist, prefer an Apple result that matches.
        // This prevents playing the wrong "artist variant" when titles collide.
        let song = results[0];
        try {
            const ds = (datasetMeta && typeof datasetMeta === 'object') ? datasetMeta : null;
            const dsTitle = (ds?.title || '').trim().toLowerCase();
            const dsArtist = (ds?.artist || '').trim().toLowerCase();
            if (dsTitle || dsArtist) {
                const norm = (s) => (s || '').toString().trim().toLowerCase();
                const looseIncludes = (a, b) => {
                    const aa = norm(a);
                    const bb = norm(b);
                    if (!aa || !bb) return false;
                    return aa.includes(bb) || bb.includes(aa);
                };

                const best = results.find(r => {
                    const attrs = r?.attributes || {};
                    const at = norm(attrs.name);
                    const aa = norm(attrs.artistName);
                    const titleOk = dsTitle ? looseIncludes(at, dsTitle) : true;
                    const artistOk = dsArtist ? looseIncludes(aa, dsArtist) : true;
                    return titleOk && artistOk;
                });

                if (best) {
                    song = best;
                } else {
                console.warn('No Apple Music result matched dataset title/artist; will try next fallback candidate.', { query, datasetMeta });
                    return false;
                }
            }
        } catch (e) {
            // fall back to first result
            song = results[0];
        }

        // If we have dataset sliders, prime the meta card immediately (never show "no match"
        // for DJ-picked songs). We'll still try to replace it with DB-backed weights later.
        try {
            if (datasetMeta && typeof datasetMeta === 'object' && datasetMeta.sliders) {
                renderSongMetaFromDatasetPayload(datasetMeta);
            }
        } catch (e) {
            // ignore
        }
        
        const nowPlaying = musicKit.nowPlayingItem;
        const queueBefore = getQueueLength();
        
        console.log(`🎵 Queue command received: "${song.attributes.name}" by ${song.attributes.artistName}`);
        console.log(`🎵 Current state: playing=${musicKit.isPlaying}, nowPlaying=${nowPlaying?.title || 'none'}, queueLength=${queueBefore}`);
        logQueueContents();  // Debug: show full queue state
        
        // If nothing is currently playing, just play directly
        if (!musicKit.isPlaying && !nowPlaying) {
            console.log('🎵 Nothing playing, starting directly');
            await playSong(song);
        } else {
            // Add to queue to play next
            try {
                await musicKit.playNext({ song: song.id });
                const queueAfter = getQueueLength();
                console.log(`🎵 Queued via playNext, queue: ${queueBefore} → ${queueAfter}`);
            } catch (queueErr) {
                // Fallback: if playNext fails, try setQueue
                console.warn('playNext failed, trying setQueue fallback:', queueErr);
                await musicKit.setQueue({ songs: [song.id], startPlaying: false });
                console.log('🎵 Added to queue via setQueue');
            }
        }
        
        // Track this song as DJ-queued
        djQueuedSongs.push({
            title: song.attributes.name,
            artist: song.attributes.artistName,
            id: song.id,
            dataset: (datasetMeta && typeof datasetMeta === 'object') ? datasetMeta : null,
        });
        console.log(`🎵 DJ queue tracking: ${djQueuedSongs.length} songs pending`);
        
        // Notify backend that song was queued (single source of truth for recording)
        console.log(`🎵 Emitting music_song_queued: "${song.attributes.name}" by ${song.attributes.artistName}`);
        socket.emit('music_song_queued', {
            title: song.attributes.name,
            artist: song.attributes.artistName,
            query: query,
        });
        
        // Reset pending flag - request fulfilled
        pickRequestPending = false;
        setDjThinking(false);
        
        return true;
    } catch (err) {
        console.error('Failed to queue song:', err);
        if (isAuthError(err)) {
            await forceReauthorize('Your session has ended. Sign in again.');
        }
        pickRequestPending = false;
        setDjThinking(false);
        return false;
    }
}

async function queueNextSongFromCandidates(candidates) {
    if (!Array.isArray(candidates) || candidates.length === 0) return false;
    for (const c of candidates) {
        if (!c || typeof c !== 'object') continue;
        const q = (c.search_query || (c.title && c.artist ? `${c.title} by ${c.artist}` : '') || '').toString().trim();
        if (!q) continue;
        const ok = await queueNextSong(q, c);
        if (ok) return true;
    }
    return false;
}

/**
 * Play a specific song
 */
async function playSong(song) {
    if (!musicKit || !isAuthorized) return;
    
    try {
        await musicKit.setQueue({ song: song.id });
        await musicKit.play();
        console.log('🎵 Playing:', song.attributes.name);
    } catch (err) {
        console.error('Failed to play song:', err);
        if (isAuthError(err)) {
            await forceReauthorize('Your session has ended. Sign in again.');
        }
    }
}

/**
 * Display search results
 */
function displaySearchResults(songs) {
    if (!searchResults) return;
    
    if (songs.length === 0) {
        searchResults.innerHTML = '<p style="color: var(--music-text-secondary); text-align: center; padding: 20px;">No results found</p>';
        return;
    }
    
    searchResults.innerHTML = songs.map(song => {
        const attrs = song.attributes;
        const artworkUrl = attrs.artwork?.url?.replace('{w}', '100').replace('{h}', '100') || '';
        
        return `
            <div class="search-result-item" data-song-id="${song.id}">
                <div class="search-result-art">
                    ${artworkUrl ? `<img src="${artworkUrl}" alt="">` : ''}
                </div>
                <div class="search-result-info">
                    <div class="search-result-title">${attrs.name}</div>
                    <div class="search-result-artist">${attrs.artistName}</div>
                </div>
            </div>
        `;
    }).join('');
    
    // Add click handlers
    searchResults.querySelectorAll('.search-result-item').forEach((item, index) => {
        item.addEventListener('click', () => playSong(songs[index]));
    });
}

// =============================================================================
// DJ Mode Functions
// =============================================================================

/**
 * Toggle DJ mode on/off
 */
async function toggleDJMode(enabled) {
    // Frontend is authoritative: DJ mode == auto-pick loop.
    djModeEnabled = !!enabled;
    try {
        localStorage.setItem(DJ_MODE_STORAGE_KEY, djModeEnabled ? '1' : '0');
    } catch (e) {
        // ignore
    }

    // Start/stop progress reporting immediately (don't wait for backend).
    if (djModeEnabled) {
        startProgressReporting();
        setTimeout(() => checkQueueNeedsSongs(), 50);
    } else {
        stopProgressReporting();
        pickRequestPending = false;
        setDjThinking(false);
    }

    try {
        const endpoint = enabled ? '/api/music/dj/enable' : '/api/music/dj/disable';
        const response = await fetch(endpoint, { method: 'POST' });
        const data = await response.json();
        updateDJModeUI(data.dj);
    } catch (err) {
        console.error('Failed to toggle DJ mode:', err);
    }
}

function setPauseOnAfk(enabled) {
    pauseOnAfkEnabled = !!enabled;
    savePauseOnAfkSetting(pauseOnAfkEnabled);
    updateDJModeUI({ enabled: djModeEnabled });
}

/**
 * Fetch and update DJ mode status
 */
async function fetchDJStatus() {
    try {
        const response = await fetch('/api/music/dj/status');
        const data = await response.json();

        const toggle = document.getElementById('dj-mode-toggle');
        if (toggle) {
            toggle.checked = djModeEnabled;
        }
        
        updateDJModeUI(data);

        // Keep the periodic queue checks aligned with frontend policy (important on page reload).
        if (djModeEnabled) {
            startProgressReporting();
            setTimeout(() => checkQueueNeedsSongs(), 50);
        } else {
            stopProgressReporting();
            pickRequestPending = false;
            setDjThinking(false);
        }
    } catch (err) {
        console.error('Failed to fetch DJ status:', err);
    }
}

/**
 * Update DJ mode UI elements
 */
function updateDJModeUI(djData) {
    const statsEl = document.getElementById('dj-stats');
    const statsText = document.getElementById('dj-stats-text');
    const actionsEl = document.getElementById('dj-actions');
    const pauseOnAfkToggle = document.getElementById('dj-pause-on-afk-toggle');
    const pickBtn = document.getElementById('dj-pick-btn');
    
    if (djData) {
        // UI should respond immediately when the toggle succeeds.
        // Backend enable happens on a separate DJ thread, so `djData.enabled` can lag briefly.
        const uiEnabled = !!djData.enabled;

        // "Let DJ Pick" is always visible; DJ mode only controls auto-pick + AFK behavior.
        if (actionsEl) actionsEl.classList.remove('hidden');

        if (statsEl) {
            if (uiEnabled) statsEl.classList.remove('hidden');
            else statsEl.classList.add('hidden');
        }

        if (statsText) {
            const stats = djData.stats || {};
            const autoPick = djModeEnabled ? 'ON' : 'OFF';
            statsText.textContent = `Auto-pick: ${autoPick} | Pauses: ${stats.afk_pauses || 0} | Resumes: ${stats.afk_resumes || 0}`;
        }

        if (pauseOnAfkToggle) {
            pauseOnAfkToggle.checked = !!pauseOnAfkEnabled;
            pauseOnAfkToggle.disabled = !isAuthorized;
        }

        if (pickBtn) {
            // One-shot pick button is always available when authorized.
            pickBtn.disabled = !isAuthorized;
            const label = pickBtn.querySelector('span');
            if (label && !pickBtn.classList.contains('loading')) {
                label.textContent = uiEnabled ? 'Pick now' : 'Let DJ Pick';
            }
        }
    }
}

/**
 * Have the DJ pick a song based on current context
 */
async function djPickSong() {
    const btn = document.getElementById('dj-pick-btn');
    if (!btn) return;
    
    if (!isAuthorized) {
        alert('Please authorize Apple Music first');
        return;
    }

    btn.classList.add('loading');
    btn.querySelector('span').textContent = 'Thinking...';
    pickRequestPending = true;
    setDjThinking(true, 'Thinking...');
    // Safety: clear pending if something goes wrong (LLM hung, socket lost, etc.)
    setTimeout(() => {
        if (pickRequestPending) {
            pickRequestPending = false;
            setDjThinking(false);
        }
    }, 60000);
    
    try {
        // One-shot pick is allowed even when DJ mode is OFF.
        const response = await fetch('/api/music/dj/pick_once', { method: 'POST' });
        const data = await response.json();
        
        if (data.error) {
            console.error('DJ pick error:', data.error);
            btn.querySelector('span').textContent = 'Error';
            pickRequestPending = false;
            setDjThinking(false);
        } else if (data.status === 'skipped') {
            btn.querySelector('span').textContent = 'DJ Skipped';
            pickRequestPending = false;
            setDjThinking(false);
        } else {
            btn.querySelector('span').textContent = 'Queued';
            // Keep thinking until the queue_next command is actually processed.
        }

        // Meta is rendered from DJ payload (no lookups).
        
        // Reset button after 2 seconds
        setTimeout(() => {
            btn.classList.remove('loading');
            btn.querySelector('span').textContent = djModeEnabled ? 'Pick now' : 'Let DJ Pick';
        }, 2000);
        
    } catch (err) {
        console.error('Failed to pick song:', err);
        btn.classList.remove('loading');
        btn.querySelector('span').textContent = djModeEnabled ? 'Pick now' : 'Let DJ Pick';
        pickRequestPending = false;
        setDjThinking(false);
    }
}

/**
 * Set up UI event handlers
 */
function setupUIHandlers() {
    // DJ Mode toggle
    const djToggle = document.getElementById('dj-mode-toggle');
    if (djToggle) {
        djToggle.addEventListener('change', (e) => {
            toggleDJMode(e.target.checked);
        });
    }

    const pauseOnAfkToggle = document.getElementById('dj-pause-on-afk-toggle');
    if (pauseOnAfkToggle) {
        pauseOnAfkToggle.addEventListener('change', (e) => {
            setPauseOnAfk(e.target.checked);
        });
    }
    
    // DJ Pick button
    const djPickBtn = document.getElementById('dj-pick-btn');
    if (djPickBtn) {
        djPickBtn.addEventListener('click', djPickSong);
    }

    // Meta decrement buttons
    if (btnDecrTrack) btnDecrTrack.addEventListener('click', () => decrementWeight('track'));
    if (btnDecrArtist) btnDecrArtist.addEventListener('click', () => decrementWeight('artist'));
    if (btnDecrGenre) btnDecrGenre.addEventListener('click', () => decrementWeight('genre'));
    if (btnIncrTrack) btnIncrTrack.addEventListener('click', () => incrementWeight('track'));
    if (btnIncrArtist) btnIncrArtist.addEventListener('click', () => incrementWeight('artist'));
    if (btnIncrGenre) btnIncrGenre.addEventListener('click', () => incrementWeight('genre'));
    if (btnBanTrack) btnBanTrack.addEventListener('click', () => banWeight('track'));
    if (btnBanArtist) btnBanArtist.addEventListener('click', () => banWeight('artist'));
    if (btnBanGenre) btnBanGenre.addEventListener('click', () => banWeight('genre'));
    
    // Fetch Taste button
    const fetchTasteBtn = document.getElementById('fetch-taste-btn');
    if (fetchTasteBtn) {
        fetchTasteBtn.addEventListener('click', fetchMusicTaste);
    }
    
    // Copy Taste button
    const copyTasteBtn = document.getElementById('copy-taste-btn');
    if (copyTasteBtn) {
        copyTasteBtn.addEventListener('click', copyTasteToClipboard);
    }
    
    // Authorize button
    if (authorizeBtn) {
        authorizeBtn.addEventListener('click', authorize);
    }
    
    // Play/Pause button
    const btnPlayPause = document.getElementById('btn-play-pause');
    if (btnPlayPause) {
        btnPlayPause.addEventListener('click', () => {
            if (musicKit?.isPlaying) {
                musicKit.pause();
            } else {
                musicKit?.play();
            }
        });
    }
    
    // Previous button
    const btnPrevious = document.getElementById('btn-previous');
    if (btnPrevious) {
        btnPrevious.addEventListener('click', () => {
            musicKit?.skipToPreviousItem();
        });
    }
    
    // Next button
    const btnNext = document.getElementById('btn-next');
    if (btnNext) {
        btnNext.addEventListener('click', async () => {
            try {
                await musicKit?.skipToNextItem();
            } catch (err) {
                console.warn('skipToNextItem failed:', err);
            } finally {
                // Skipping can leave the queue empty; if DJ auto-pick is on, refill quickly.
                setTimeout(() => checkQueueNeedsSongs(), 200);
            }
        });
    }
    
    // Volume slider
    if (volumeSlider) {
        volumeSlider.addEventListener('input', (e) => {
            if (musicKit) {
                musicKit.volume = e.target.value / 100;
            }
        });
    }
    
    // Progress bar click to seek
    if (progressBar) {
        progressBar.addEventListener('click', (e) => {
            if (!musicKit || !musicKit.currentPlaybackDuration) return;
            
            const rect = progressBar.getBoundingClientRect();
            const percent = (e.clientX - rect.left) / rect.width;
            const seekTime = percent * musicKit.currentPlaybackDuration;
            
            musicKit.seekToTime(seekTime);
        });
    }
    
    // Search input
    if (searchInput) {
        let searchTimeout;
        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            const query = e.target.value.trim();
            
            if (query.length < 2) {
                if (searchResults) searchResults.innerHTML = '';
                return;
            }
            
            searchTimeout = setTimeout(async () => {
                const results = await searchSongs(query);
                displaySearchResults(results);
            }, 300);
        });
        
        // Search on Enter
        searchInput.addEventListener('keypress', async (e) => {
            if (e.key === 'Enter') {
                const query = e.target.value.trim();
                if (query) {
                    const results = await searchSongs(query);
                    if (results.length > 0) {
                        await playSong(results[0]);
                    }
                }
            }
        });
    }
    
    // Search button
    const searchBtn = document.getElementById('search-btn');
    if (searchBtn && searchInput) {
        searchBtn.addEventListener('click', async () => {
            const query = searchInput.value.trim();
            if (query) {
                const results = await searchSongs(query);
                displaySearchResults(results);
            }
        });
    }
}

/**
 * Initialize everything on page load
 */
document.addEventListener('DOMContentLoaded', () => {
    console.log('🎵 Music Player initializing...');

    // Load frontend-only settings
    loadPauseOnAfkSetting();
    try {
        const rawDj = localStorage.getItem(DJ_MODE_STORAGE_KEY);
        if (rawDj !== null) djModeEnabled = rawDj === '1' || rawDj === 'true';
    } catch (e) {
        // ignore
    }
    
    // Initialize socket connection
    initSocket();
    
    // Set up UI handlers
    setupUIHandlers();
    
    // Initialize MusicKit
    initMusicKit();
    
    // Fetch DJ mode status
    fetchDJStatus();
    
    // Periodically update DJ stats (every 30s)
    setInterval(() => {
        if (djModeEnabled) {
            fetchDJStatus();
        }
    }, 30000);
});
