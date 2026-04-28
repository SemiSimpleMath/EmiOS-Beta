/**
 * Proactive Suggestion Popup
 * ==========================
 * 
 * Displays multiple proactive suggestions in a single floating popup.
 * Users can Accept, Snooze, or Dismiss each suggestion individually.
 * TTS is handled server-side via WebSocket.
 */

class ProactiveSuggestionPopup {
    constructor() {
        this.suggestions = [];  // All pending suggestions shown at once
        this.pollInterval = null;
        this.activePlanSession = null;
        this.snoozeOptions = [
            { label: '15 min', value: 15 },
            { label: '30 min', value: 30 },
            { label: '1 hr', value: 60 },
            { label: '2 hr', value: 120 }
        ];
        
        this.init();
    }
    
    init() {
        this.createPopupElement();
        this.setupSocketListener();
        this.startPolling(30000);
        this.checkForSuggestions();
        console.log('ProactiveSuggestionPopup initialized');
    }
    
    setupSocketListener() {
        if (typeof socket !== 'undefined') {
            socket.on('proactive_suggestion', (data) => {
                console.log('Received proactive_suggestion:', data);
                this.handleNewSuggestion(data);
            });
            
            socket.on('proactive_suggestion_update', (data) => {
                this.checkForSuggestions();
            });
        }
    }
    
    handleNewSuggestion(suggestion) {
        // Check for duplicates
        const existingIds = new Set(this.suggestions.map(s => s.ticket_id));
        if (existingIds.has(suggestion.ticket_id)) return;
        
        this.suggestions.push(suggestion);
        this.render();
    }
    
    createPopupElement() {
        if (document.getElementById('proactive-popup')) return;
        
        const popup = document.createElement('div');
        popup.id = 'proactive-popup';
        popup.className = 'proactive-popup hidden';
        popup.innerHTML = `
            <div class="proactive-popup-header">
                <span class="proactive-popup-icon">${window.EMI_ASSISTANT_NAME || 'Assistant'}</span>
                <span class="proactive-popup-title">Suggestions</span>
                <span class="proactive-popup-count"></span>
                <button class="proactive-popup-close" title="Dismiss All">X</button>
            </div>
            <div class="proactive-popup-list"></div>
        `;
        
        document.body.appendChild(popup);
        this.createPlanModalElement();
        
        // Dismiss all button
        popup.querySelector('.proactive-popup-close').addEventListener('click', () => {
            this.dismissAll();
        });
    }

    createPlanModalElement() {
        if (document.getElementById('proactive-plan-modal')) return;
        const modal = document.createElement('div');
        modal.id = 'proactive-plan-modal';
        modal.className = 'proactive-plan-modal hidden';
        modal.innerHTML = `
            <div class="proactive-plan-panel">
                <div class="proactive-plan-header">
                    <div class="proactive-plan-title">Plan Mode</div>
                    <button class="proactive-plan-close" title="Close">X</button>
                </div>
                <div class="proactive-plan-context">
                    <div class="proactive-plan-context-title"></div>
                    <div class="proactive-plan-context-message"></div>
                </div>
                <div class="proactive-plan-chat-log"></div>
                <div class="proactive-plan-input-row">
                    <input type="text" class="proactive-plan-input" placeholder="Type message to orchestrator..." maxlength="1000">
                    <button class="proactive-btn-sm proactive-plan-send">Send</button>
                </div>
                <div class="proactive-plan-actions">
                    <button class="proactive-btn-sm proactive-plan-done">Done</button>
                    <button class="proactive-btn-sm proactive-plan-cancel">Cancel</button>
                </div>
            </div>
        `;
        document.body.appendChild(modal);

        modal.querySelector('.proactive-plan-close').addEventListener('click', () => this.closePlanModal());
        modal.querySelector('.proactive-plan-send').addEventListener('click', () => this.sendPlanChat());
        modal.querySelector('.proactive-plan-done').addEventListener('click', () => this.finishPlanMode('done'));
        modal.querySelector('.proactive-plan-cancel').addEventListener('click', () => this.finishPlanMode('cancel'));
        modal.querySelector('.proactive-plan-input').addEventListener('keydown', (ev) => {
            if (ev.key === 'Enter') {
                ev.preventDefault();
                this.sendPlanChat();
            }
        });
    }
    
    startPolling(intervalMs = 30000) {
        this.pollInterval = setInterval(() => this.checkForSuggestions(), intervalMs);
    }
    
    isUserInteracting() {
        // Check if user has focus on any element inside the popup
        const popup = document.getElementById('proactive-popup');
        if (!popup) return false;
        return popup.contains(document.activeElement) && document.activeElement !== popup;
    }
    
    async checkForSuggestions() {
        // Skip polling if user is actively interacting with the popup
        if (this.isUserInteracting()) {
            console.log('[ProactivePopup] Skipping poll - user is interacting');
            return;
        }
        
        try {
            const response = await fetch('/api/tickets/pending');
            const data = await response.json();
            
            console.log('[ProactivePopup] Fetched pending suggestions:', data);
            
            const serverIds = new Set((data.tickets || []).map(s => s.ticket_id));
            const existingIds = new Set(this.suggestions.map(s => s.ticket_id));
            
            // Remove suggestions that are no longer on server (expired, dismissed elsewhere, etc.)
            const removed = this.suggestions.filter(s => !serverIds.has(s.ticket_id));
            if (removed.length > 0) {
                this.suggestions = this.suggestions.filter(s => serverIds.has(s.ticket_id));
                console.log('[ProactivePopup] Removed expired/dismissed:', removed.map(s => s.ticket_id));
            }
            
            // Add new suggestions from server
            for (const suggestion of (data.tickets || [])) {
                if (!existingIds.has(suggestion.ticket_id)) {
                    this.suggestions.push(suggestion);
                    console.log('[ProactivePopup] Added suggestion:', suggestion.ticket_id);
                }
            }
            
            this.render();
        } catch (e) {
            console.error('[ProactivePopup] Error checking for suggestions:', e);
        }
    }
    
    _resolveLayout(s) {
        switch ((s.ticket_type || '').toLowerCase()) {
            case 'cron_reminder':    return { layout: 'activity',      planMode: false };
            case 'tool_approval':    return { layout: 'tool_approval', planMode: false };
            case 'dayflow_notify':   return { layout: 'notify',        planMode: false };
            case 'dayflow_decision': return { layout: 'decision',      planMode: true  };
            default:                 return { layout: 'advice',        planMode: true  };
        }
    }

    render() {
        const popup = document.getElementById('proactive-popup');
        if (!popup) return;
        
        const listContainer = popup.querySelector('.proactive-popup-list');
        const countEl = popup.querySelector('.proactive-popup-count');
        
        if (this.suggestions.length === 0) {
            popup.classList.add('hidden');
            return;
        }
        
        // Update count
        countEl.textContent = `(${this.suggestions.length})`;
        
        // Capture current text input values before re-rendering (to prevent text loss during polling)
        const userTextInputs = listContainer.querySelectorAll('.proactive-user-text');
        const userTextValues = {};
        userTextInputs.forEach(input => {
            const ticketId = input.closest('.proactive-item').dataset.ticketId;
            if (input.value) {
                userTextValues[ticketId] = input.value;
            }
        });
        
        // Build list HTML
        listContainer.innerHTML = this.suggestions.map((s, idx) => {
            const { layout, planMode } = this._resolveLayout(s);
            const isToolApproval = layout === 'tool_approval';
            const isNotify    = layout === 'notify';
            const isDecision  = layout === 'decision';
            const isAdvice    = layout === 'advice';
            
            let buttonHtml;
            if (isToolApproval) {
                buttonHtml = `
                    <button class="proactive-btn-sm proactive-btn-accept" data-idx="${idx}">Allow</button>
                    <button class="proactive-btn-sm proactive-btn-dismiss" data-idx="${idx}">Deny</button>
                `;
            } else if (isNotify) {
                buttonHtml = `
                    <div class="proactive-actions-row">
                        <button class="proactive-btn-sm proactive-btn-ok" data-idx="${idx}">OK</button>
                    </div>
                `;
            } else if (isDecision) {
                // Plan Mode button suppressed — the ticket-driven plan-mode
                // flow is currently unused. Modal + handlers kept for easy
                // resuscitation; just not rendered. See startPlanMode() etc.
                const planModeHtml = '';
                buttonHtml = `
                    <div class="proactive-actions-row">
                        <button class="proactive-btn-sm proactive-btn-allow" data-idx="${idx}">Allow</button>
                        <button class="proactive-btn-sm proactive-btn-deny" data-idx="${idx}">Deny</button>
                        ${planModeHtml}
                    </div>
                `;
            } else if (isAdvice) {
                // Advice layout: Acknowledge, Will Do, No, Later
                const planModeHtml = '';
                buttonHtml = `
                    <div class="proactive-text-container">
                        <input type="text" 
                               class="proactive-user-text" 
                               data-idx="${idx}"
                               placeholder="Optional: Add a note"
                               maxlength="200">
                    </div>
                    <div class="proactive-actions-row">
                        <button class="proactive-btn-sm proactive-btn-acknowledge" data-idx="${idx}" title="Got it, will consider">👍 Acknowledge</button>
                        <button class="proactive-btn-sm proactive-btn-willdo" data-idx="${idx}" title="I'll do this now">✓ Will Do</button>
                        <button class="proactive-btn-sm proactive-btn-no" data-idx="${idx}" title="Not applicable">✗ No</button>
                        <select class="proactive-snooze-select" data-idx="${idx}">
                            ${this.snoozeOptions.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
                        </select>
                        <button class="proactive-btn-sm proactive-btn-later" data-idx="${idx}">⏰ Later</button>
                        ${planModeHtml}
                    </div>
                `;
            } else {
                // Activity layout: Done, Skip, Later (original)
                buttonHtml = `
                    <div class="proactive-text-container">
                        <input type="text" 
                               class="proactive-user-text" 
                               data-idx="${idx}"
                               placeholder="Optional: Add details (e.g., 'Had 2 glasses')"
                               maxlength="200">
                    </div>
                    <div class="proactive-actions-row">
                        <button class="proactive-btn-sm proactive-btn-done" data-idx="${idx}">✓ Done</button>
                        <button class="proactive-btn-sm proactive-btn-skip" data-idx="${idx}">✗ Skip</button>
                        <select class="proactive-snooze-select" data-idx="${idx}">
                            ${this.snoozeOptions.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
                        </select>
                        <button class="proactive-btn-sm proactive-btn-later" data-idx="${idx}">⏰ Later</button>
                    </div>
                `;
            }
            
            return `
                <div class="proactive-item ${isToolApproval ? 'tool-approval' : ''}" data-ticket-id="${s.ticket_id}">
                    <button class="proactive-item-close" data-idx="${idx}" title="Close without action">&times;</button>
                    <div class="proactive-item-content">
                        <div class="proactive-item-title">${this.escapeHtml(s.title || 'Suggestion')}</div>
                        <div class="proactive-item-message">${isToolApproval ? this.formatApprovalMessage(s.message || '') : this.escapeHtml(s.message || '')}</div>
                        <div class="proactive-item-meta">${isToolApproval ? '🔐 Tool Approval' : (s.suggestion_type || '')}</div>
                    </div>
                    <div class="proactive-item-actions">
                        ${buttonHtml}
                    </div>
                </div>
            `;
        }).join('');
        
        // Restore text input values after re-rendering
        listContainer.querySelectorAll('.proactive-user-text').forEach(input => {
            const ticketId = input.closest('.proactive-item').dataset.ticketId;
            if (userTextValues[ticketId]) {
                input.value = userTextValues[ticketId];
            }
        });
        
        // Bind action buttons
        listContainer.querySelectorAll('.proactive-item-close').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'close'));
        });
        listContainer.querySelectorAll('.proactive-btn-accept').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'accept'));
        });
        listContainer.querySelectorAll('.proactive-btn-done').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'done'));
        });
        listContainer.querySelectorAll('.proactive-btn-skip').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'skip'));
        });
        listContainer.querySelectorAll('.proactive-btn-later').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'later'));
        });
        listContainer.querySelectorAll('.proactive-btn-dismiss').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'dismiss'));
        });
        listContainer.querySelectorAll('.proactive-btn-ok').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'acknowledge'));
        });
        listContainer.querySelectorAll('.proactive-btn-allow').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'accept'));
        });
        listContainer.querySelectorAll('.proactive-btn-deny').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'dismiss'));
        });
        // Advice layout buttons
        listContainer.querySelectorAll('.proactive-btn-acknowledge').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'acknowledge'));
        });
        listContainer.querySelectorAll('.proactive-btn-willdo').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'willdo'));
        });
        listContainer.querySelectorAll('.proactive-btn-no').forEach(btn => {
            btn.addEventListener('click', () => this.respond(parseInt(btn.dataset.idx), 'no'));
        });
        listContainer.querySelectorAll('.proactive-btn-plan-mode').forEach(btn => {
            btn.addEventListener('click', () => this.startPlanMode(parseInt(btn.dataset.idx)));
        });
        
        popup.classList.remove('hidden');
    }

    getSocketId() {
        try {
            if (typeof socket !== 'undefined' && socket && typeof socket.id === 'string' && socket.id) {
                return socket.id;
            }
        } catch (_) {}
        return '';
    }

    getPlanModalElement() {
        return document.getElementById('proactive-plan-modal');
    }

    appendPlanMessage(role, text) {
        const modal = this.getPlanModalElement();
        if (!modal) return;
        const log = modal.querySelector('.proactive-plan-chat-log');
        if (!log) return;
        const bubble = document.createElement('div');
        bubble.className = `proactive-plan-msg proactive-plan-msg-${role === 'assistant' ? 'assistant' : 'user'}`;
        bubble.textContent = String(text || '');
        log.appendChild(bubble);
        log.scrollTop = log.scrollHeight;
    }

    openPlanModal(session, suggestion) {
        const modal = this.getPlanModalElement();
        if (!modal) return;
        const roomId = String(session.room_id || '').trim();
        if (!roomId) {
            console.error('Plan session missing room_id:', session);
            return;
        }
        this.activePlanSession = {
            plan_session_id: session.plan_session_id,
            ticket_id: session.ticket_id,
            room_id: roomId,
            room_context_id: session.room_context_id || `plan::${session.ticket_id}`
        };
        const titleEl = modal.querySelector('.proactive-plan-context-title');
        const messageEl = modal.querySelector('.proactive-plan-context-message');
        const log = modal.querySelector('.proactive-plan-chat-log');
        const input = modal.querySelector('.proactive-plan-input');
        if (titleEl) titleEl.textContent = suggestion.title || 'Plan Session';
        if (messageEl) messageEl.textContent = suggestion.message || '';
        if (log) log.innerHTML = '';
        this.appendPlanMessage('assistant', 'Plan mode started. Tell me what you want to do, and when ready click Done.');
        modal.classList.remove('hidden');
        if (input) input.focus();
    }

    closePlanModal() {
        const modal = this.getPlanModalElement();
        if (!modal) return;
        modal.classList.add('hidden');
    }

    async startPlanMode(idx) {
        const suggestion = this.suggestions[idx];
        if (!suggestion) return;
        const ticketId = suggestion.ticket_id;
        try {
            const response = await fetch('/api/tickets/plan/start', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ticket_id: ticketId,
                    socket_id: this.getSocketId()
                })
            });
            if (!response.ok) {
                console.error('Error starting plan mode:', await response.text());
                return;
            }
            const data = await response.json();
            this.suggestions.splice(idx, 1);
            this.render();
            this.openPlanModal(data, suggestion);
        } catch (e) {
            console.error('Error starting plan mode:', e);
        }
    }

    async sendPlanChat() {
        const modal = this.getPlanModalElement();
        if (!modal || !this.activePlanSession) return;
        const input = modal.querySelector('.proactive-plan-input');
        const sendBtn = modal.querySelector('.proactive-plan-send');
        const text = input ? input.value.trim() : '';
        if (!text) return;
        if (input) input.value = '';
        this.appendPlanMessage('user', text);
        if (sendBtn) sendBtn.disabled = true;
        try {
            const response = await fetch('/api/tickets/plan/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ticket_id: this.activePlanSession.ticket_id,
                    plan_session_id: this.activePlanSession.plan_session_id,
                    text: text,
                    socket_id: this.getSocketId(),
                    sender_name: 'User'
                })
            });
            if (!response.ok) {
                const err = await response.text();
                this.appendPlanMessage('assistant', `Error: ${err}`);
                return;
            }
            const data = await response.json();
            const reply = (data.reply_text || '').trim();
            if (reply) {
                this.appendPlanMessage('assistant', reply);
            }
        } catch (e) {
            this.appendPlanMessage('assistant', `Error: ${String(e)}`);
            console.error('Error in plan mode chat:', e);
        } finally {
            if (sendBtn) sendBtn.disabled = false;
            if (input) input.focus();
        }
    }

    async finishPlanMode(mode) {
        if (!this.activePlanSession) {
            this.closePlanModal();
            return;
        }
        const endpoint = mode === 'done' ? '/api/tickets/plan/done' : '/api/tickets/plan/cancel';
        try {
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ticket_id: this.activePlanSession.ticket_id,
                    plan_session_id: this.activePlanSession.plan_session_id
                })
            });
            if (!response.ok) {
                console.error(`Error finishing plan mode (${mode}):`, await response.text());
                return;
            }
            this.activePlanSession = null;
            this.closePlanModal();
            this.checkForSuggestions();
        } catch (e) {
            console.error(`Error finishing plan mode (${mode}):`, e);
        }
    }
    
    async respond(idx, action) {
        const suggestion = this.suggestions[idx];
        if (!suggestion) return;
        
        // Prevent double-clicks - mark as processing immediately
        const ticketId = suggestion.ticket_id;
        if (suggestion._processing) return;
        suggestion._processing = true;
        
        // Get user text from input box (before removing from DOM)
        const popup = document.getElementById('proactive-popup');
        const textInput = popup.querySelector(`.proactive-user-text[data-idx="${idx}"]`);
        const userText = textInput ? textInput.value.trim() : '';
        
        // Get snooze value if applicable
        const selectEl = popup.querySelector(`.proactive-snooze-select[data-idx="${idx}"]`);
        const snoozeMinutes = selectEl ? parseInt(selectEl.value) : 30;
        
        // Remove from UI immediately (optimistic update)
        this.suggestions.splice(idx, 1);
        this.render();
        
        try {
            const response = await fetch('/api/tickets/respond', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    ticket_id: ticketId,
                    action: action,
                    user_text: userText,
                    snooze_minutes: snoozeMinutes
                })
            });
            
            if (!response.ok) {
                console.error('Error responding to suggestion:', await response.text());
                // Re-add on error (rollback optimistic update)
                this.suggestions.splice(idx, 0, suggestion);
                this.render();
            }
        } catch (e) {
            console.error('Error responding to suggestion:', e);
            // Re-add on error
            this.suggestions.splice(idx, 0, suggestion);
            this.render();
        }
    }
    
    async dismissAll() {
        for (const suggestion of [...this.suggestions]) {
            try {
                await fetch('/api/tickets/respond', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        ticket_id: suggestion.ticket_id,
                        action: 'close',
                        user_text: '',
                        snooze_minutes: 0
                    })
                });
            } catch (e) {
                console.error('Error closing ticket:', e);
            }
        }
        this.suggestions = [];
        this.render();
    }
    
    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    formatApprovalMessage(raw) {
        // Try to pretty-print JSON if not already formatted
        let display = raw;
        try {
            const parsed = JSON.parse(raw);
            display = JSON.stringify(parsed, null, 2);
        } catch (_) {
            // not JSON — show as-is
        }
        const escaped = this.escapeHtml(display);
        return escaped;
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.proactivePopup = new ProactiveSuggestionPopup();
});
