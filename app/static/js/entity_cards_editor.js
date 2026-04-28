// Entity Cards Editor JavaScript

let currentPage = 0;
let pageSize = 50;
let currentFilters = {
    search: '',
    type: '',
    sort: 'name',
    includeInactive: false,
};
let allEntityTypes = [];
let currentCard = null;

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    loadStatistics();
    loadEntityTypes();
    loadCards();
    setupEventListeners();
});

function setupEventListeners() {
    // Form submission
    document.getElementById('entity-card-form').addEventListener('submit', handleFormSubmit);
    
    // Search input debounce
    let searchTimeout;
    document.getElementById('search-input').addEventListener('input', (e) => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            currentFilters.search = e.target.value;
            currentPage = 0;
            loadCards();
        }, 300);
    });
    
    // Filter changes
    document.getElementById('type-filter').addEventListener('change', (e) => {
        currentFilters.type = e.target.value;
        currentPage = 0;
        loadCards();
    });
    
    document.getElementById('sort-select').addEventListener('change', (e) => {
        currentFilters.sort = e.target.value;
        currentPage = 0;
        loadCards();
    });
}

// ========================================================================
// LOADING DATA
// ========================================================================

async function loadStatistics() {
    try {
        const response = await fetch('/api/entity_cards/stats');
        const data = await response.json();
        
        if (data.success) {
            const stats = data.stats;
            document.getElementById('stat-total-cards').textContent = stats.total_cards || 0;
            document.getElementById('stat-active-cards').textContent = stats.active_cards || 0;
            document.getElementById('stat-total-usage').textContent = stats.total_usage || 0;
            document.getElementById('stat-types').textContent = stats.type_stats?.length || 0;
        }
    } catch (error) {
        console.error('Error loading statistics:', error);
    }
}

async function loadEntityTypes() {
    try {
        const response = await fetch('/api/entity_cards/types');
        const data = await response.json();
        
        if (data.success) {
            allEntityTypes = data.types;
            const typeFilter = document.getElementById('type-filter');
            const typesList = document.getElementById('entity-types-list');
            
            // Clear existing options (except "All Types")
            typeFilter.innerHTML = '<option value="">All Types</option>';
            typesList.innerHTML = '';
            
            // Add types to filter dropdown and datalist
            allEntityTypes.forEach(type => {
                const option = document.createElement('option');
                option.value = type;
                typeFilter.appendChild(option);
                
                const datalistOption = document.createElement('option');
                datalistOption.value = type;
                typesList.appendChild(datalistOption);
            });
        }
    } catch (error) {
        console.error('Error loading entity types:', error);
    }
}

async function loadCards() {
    const cardsList = document.getElementById('cards-list');
    cardsList.innerHTML = '<div class="loading"><div class="spinner"></div><p>Loading entity cards...</p></div>';
    
    try {
        const params = new URLSearchParams({
            limit: pageSize,
            offset: currentPage * pageSize,
            sort: currentFilters.sort
        });
        
        if (currentFilters.search) {
            params.append('search', currentFilters.search);
        }
        if (currentFilters.type) {
            params.append('type', currentFilters.type);
        }
        if (currentFilters.includeInactive) {
            params.append('include_inactive', '1');
        }

        const response = await fetch(`/api/entity_cards?${params}`);
        const data = await response.json();
        
        if (data.cards) {
            renderCards(data.cards);
            updatePagination(data.total, data.limit, data.offset);
            updateSearchResultsInfo(data.total, data.cards.length);
        } else {
            cardsList.innerHTML = '<div class="message message-error">Error loading cards</div>';
        }
    } catch (error) {
        console.error('Error loading cards:', error);
        cardsList.innerHTML = '<div class="message message-error">Error loading entity cards: ' + error.message + '</div>';
    }
}

async function refreshCardsView({ refreshTypes = false } = {}) {
    await loadCards();
    await loadStatistics();
    if (refreshTypes) {
        await loadEntityTypes();
    }
}

function renderCards(cards) {
    const cardsList = document.getElementById('cards-list');
    
    if (cards.length === 0) {
        cardsList.innerHTML = '<div class="message message-info">No entity cards found. Create your first one!</div>';
        return;
    }
    
    cardsList.innerHTML = cards.map(card => {
        const isInactive = card.is_active === false;
        // Regenerate button is only meaningful for active cards.
        // For inactive cards, the path is Reactivate first, then the
        // nightly pipeline picks it up.
        const safeName = escapeHtml(card.entity_name);
        const regenerateBtn = isInactive
            ? ''
            : `<button onclick="regenerateCard('${card.id}', '${safeName.replace(/'/g, "\\'")}', this)" class="btn-icon" title="Regenerate this card via the v2 pipeline (replaces summary + key facts)">↻</button>`;
        const reactivateBtn = isInactive
            ? `<button onclick="reactivateCard('${card.id}')" class="btn-icon" title="Reactivate (allow nightly pipeline to inject and refresh again)" style="color:#10b981">♻️</button>`
            : `<button onclick="deleteCard('${card.id}')" class="btn-icon btn-danger" title="Deactivate (hides + blocks regeneration until reactivated)">🗑️</button>`;
        return `
        <div class="card-item ${isInactive ? 'card-inactive' : ''}" data-card-id="${card.id}" ${isInactive ? 'style="opacity:0.55"' : ''}>
            <div class="card-header">
                <h3 class="card-title">${escapeHtml(card.entity_name)}${isInactive ? ' <span class="badge badge-inactive" style="background:#44403c;color:#d6d3d1;font-size:11px;padding:2px 6px;border-radius:4px;margin-left:6px">inactive</span>' : ''}</h3>
                <div class="card-actions">
                    <button onclick="viewCard('${card.id}')" class="btn-icon" title="View details">ℹ️</button>
                    <button onclick="editCard('${card.id}')" class="btn-icon" title="Edit">✏️</button>
                    ${regenerateBtn}
                    ${reactivateBtn}
                </div>
            </div>
            <div class="card-body">
                <div class="card-meta">
                    <span class="badge badge-type">${escapeHtml(card.entity_type)}</span>
                    <span class="card-stat">Usage: ${card.usage_count}</span>
                    ${card.last_used ? `<span class="card-stat">Last used: ${formatDate(card.last_used)}</span>` : ''}
                </div>
                <p class="card-summary">${escapeHtml(card.summary.substring(0, 200))}${card.summary.length > 200 ? '...' : ''}</p>
                ${card.aliases && card.aliases.length > 0 ? `<div class="card-aliases">Aliases: ${card.aliases.map(a => escapeHtml(a)).join(', ')}</div>` : ''}
            </div>
        </div>
    `;
    }).join('');
}

function toggleShowInactive(checked) {
    currentFilters.includeInactive = !!checked;
    currentPage = 0;
    loadCards();
}

async function regenerateCard(cardId, label, btn) {
    if (!confirm(`Regenerate the entity card for "${label}"?\n\nThe v2 pipeline will rebuild summary + key facts from the current KG neighborhood. This takes 5–15 seconds.`)) {
        return;
    }
    const original = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = '⏳'; btn.title = 'Regenerating…'; }
    try {
        const response = await fetch(`/entity-cards-admin/api/regenerate_v2/${cardId}`, { method: 'POST' });
        if (response.status === 409) {
            alert('Another card-generation run is already in flight. Wait for it to finish and retry.');
            return;
        }
        const data = await response.json();
        if (data.error) {
            alert('Regenerate failed: ' + data.error);
            return;
        }
        const status = (data.result && data.result.status) || 'ok';
        if (status === 'error') {
            alert('Regenerate failed: ' + ((data.result && data.result.error) || 'unknown error'));
            return;
        }
        // Refresh the list so the new summary shows.
        await loadCards();
    } catch (e) {
        alert('Regenerate error: ' + e.message);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = original; btn.title = 'Regenerate this card via the v2 pipeline (replaces summary + key facts)'; }
    }
}

async function reactivateCard(cardId) {
    try {
        const response = await fetch(`/api/entity_cards/${cardId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_active: true }),
        });
        const data = await response.json();
        if (data.success === false) {
            alert('Failed to reactivate: ' + (data.message || 'unknown error'));
            return;
        }
        await loadCards();
    } catch (e) {
        alert('Reactivate error: ' + e.message);
    }
}

function updatePagination(total, limit, offset) {
    const pagination = document.getElementById('pagination');
    const totalPages = Math.ceil(total / limit);
    const currentPageNum = Math.floor(offset / limit) + 1;
    
    if (totalPages <= 1) {
        pagination.style.display = 'none';
        return;
    }
    
    pagination.style.display = 'flex';
    document.getElementById('page-info').textContent = `Page ${currentPageNum} of ${totalPages}`;
    document.getElementById('prev-btn').disabled = currentPageNum === 1;
    document.getElementById('next-btn').disabled = currentPageNum === totalPages;
}

function updateSearchResultsInfo(total, displayed) {
    const info = document.getElementById('search-results-info');
    if (total === displayed) {
        info.textContent = `Showing ${total} entity card${total !== 1 ? 's' : ''}`;
    } else {
        info.textContent = `Showing ${displayed} of ${total} entity card${total !== 1 ? 's' : ''}`;
    }
}

// ========================================================================
// CARD OPERATIONS
// ========================================================================

async function viewCard(cardId) {
    try {
        const response = await fetch(`/api/entity_cards/${cardId}`);
        const data = await response.json();
        
        if (data.success) {
            currentCard = data.card;
            displayCardView(data.card);
            document.getElementById('view-modal').classList.remove('hidden');
        } else {
            alert('Error loading card: ' + data.message);
        }
    } catch (error) {
        console.error('Error viewing card:', error);
        alert('Error loading card: ' + error.message);
    }
}

function displayCardView(card) {
    document.getElementById('view-modal-title').textContent = card.entity_name;
    const body = document.getElementById('view-modal-body');
    
    body.innerHTML = `
        <div class="view-section">
            <h3>Basic Information</h3>
            <p><strong>Entity Name:</strong> ${escapeHtml(card.entity_name)}</p>
            <p><strong>Entity Type:</strong> ${escapeHtml(card.entity_type)}</p>
            <p><strong>Confidence:</strong> ${card.confidence !== null ? card.confidence : 'N/A'}</p>
            ${card.source_node_id ? `<p><strong>Source Node ID:</strong> <code>${escapeHtml(card.source_node_id)}</code></p>` : ''}
        </div>
        
        ${card.user_relevance_reason ? `
        <div class="view-section">
            <h3>Level 0 (One-liner)</h3>
            <p>${escapeHtml(card.user_relevance_reason)}</p>
        </div>
        ` : ''}
        
        <div class="view-section">
            <h3>Summary</h3>
            <p>${escapeHtml(card.summary)}</p>
        </div>
        
        ${card.key_facts && card.key_facts.length > 0 ? `
        <div class="view-section">
            <h3>Key Facts</h3>
            <ul>
                ${card.key_facts.map(fact => `<li>${escapeHtml(fact)}</li>`).join('')}
            </ul>
        </div>
        ` : ''}
        
        ${card.relationships && card.relationships.length > 0 ? `
        <div class="view-section">
            <h3>Relationships</h3>
            <ul>
                ${card.relationships.map(rel => `<li>${escapeHtml(rel)}</li>`).join('')}
            </ul>
        </div>
        ` : ''}
        
        ${card.aliases && card.aliases.length > 0 ? `
        <div class="view-section">
            <h3>Aliases</h3>
            <p>${card.aliases.map(a => escapeHtml(a)).join(', ')}</p>
        </div>
        ` : ''}
        
        ${card.original_description ? `
        <div class="view-section">
            <h3>Original Description</h3>
            <p>${escapeHtml(card.original_description)}</p>
        </div>
        ` : ''}
        
        ${card.original_aliases && card.original_aliases.length > 0 ? `
        <div class="view-section">
            <h3>Original Aliases</h3>
            <p>${card.original_aliases.map(a => escapeHtml(a)).join(', ')}</p>
        </div>
        ` : ''}
        
        ${card.card_metadata && Object.keys(card.card_metadata).length > 0 ? `
        <div class="view-section">
            <h3>Metadata</h3>
            <pre>${escapeHtml(JSON.stringify(card.card_metadata, null, 2))}</pre>
        </div>
        ` : ''}
        
        <div class="view-section">
            <h3>Usage Statistics</h3>
            <p><strong>Usage Count:</strong> ${card.usage_count}</p>
            ${card.last_used ? `<p><strong>Last Used:</strong> ${formatDate(card.last_used)}</p>` : '<p><strong>Last Used:</strong> Never</p>'}
            <p><strong>Created:</strong> ${formatDate(card.created_at)}</p>
            <p><strong>Updated:</strong> ${formatDate(card.updated_at)}</p>
        </div>
    `;
}

function closeViewModal() {
    document.getElementById('view-modal').classList.add('hidden');
    currentCard = null;
}

function editFromView() {
    closeViewModal();
    if (currentCard) {
        editCard(currentCard.id);
    }
}

async function editCard(cardId) {
    try {
        const response = await fetch(`/api/entity_cards/${cardId}`);
        const data = await response.json();
        
        if (data.success) {
            populateEditForm(data.card);
            document.getElementById('modal-title').textContent = 'Edit Entity Card';
            document.getElementById('edit-modal').classList.remove('hidden');
        } else {
            alert('Error loading card: ' + data.message);
        }
    } catch (error) {
        console.error('Error loading card for edit:', error);
        alert('Error loading card: ' + error.message);
    }
}

function openCreateModal() {
    document.getElementById('entity-card-form').reset();
    document.getElementById('card-id').value = '';
    document.getElementById('modal-title').textContent = 'Create New Entity Card';
    document.getElementById('edit-modal').classList.remove('hidden');
}

function closeEditModal() {
    document.getElementById('edit-modal').classList.add('hidden');
    document.getElementById('entity-card-form').reset();
}

function populateEditForm(card) {
    document.getElementById('card-id').value = card.id;
    document.getElementById('entity-name').value = card.entity_name || '';
    document.getElementById('entity-type').value = card.entity_type || '';
    document.getElementById('user-relevance-reason').value = card.user_relevance_reason || '';
    document.getElementById('summary').value = card.summary || '';
    document.getElementById('key-facts').value = (card.key_facts || []).join('\n');
    document.getElementById('relationships').value = (card.relationships || []).join('\n');
    document.getElementById('aliases').value = (card.aliases || []).join(', ');
    document.getElementById('original-description').value = card.original_description || '';
    document.getElementById('original-aliases').value = (card.original_aliases || []).join(', ');
    document.getElementById('confidence').value = card.confidence || '';
    document.getElementById('source-node-id').value = card.source_node_id || '';
    document.getElementById('card-metadata').value = card.card_metadata ? JSON.stringify(card.card_metadata, null, 2) : '{}';
}

async function handleFormSubmit(e) {
    e.preventDefault();
    
    const cardId = document.getElementById('card-id').value;
    const formData = {
        entity_name: document.getElementById('entity-name').value.trim(),
        entity_type: document.getElementById('entity-type').value.trim(),
        user_relevance_reason: document.getElementById('user-relevance-reason').value.trim(),
        summary: document.getElementById('summary').value.trim(),
        key_facts: document.getElementById('key-facts').value.split('\n').filter(f => f.trim()),
        relationships: document.getElementById('relationships').value.split('\n').filter(r => r.trim()),
        aliases: document.getElementById('aliases').value.split(',').map(a => a.trim()).filter(a => a),
        original_description: document.getElementById('original-description').value.trim() || null,
        original_aliases: document.getElementById('original-aliases').value.split(',').map(a => a.trim()).filter(a => a),
        confidence: parseFloat(document.getElementById('confidence').value) || null,
        source_node_id: document.getElementById('source-node-id').value.trim() || null
    };
    
    // Parse metadata JSON
    try {
        const metadataText = document.getElementById('card-metadata').value.trim();
        formData.card_metadata = metadataText ? JSON.parse(metadataText) : {};
    } catch (error) {
        alert('Invalid JSON in metadata field: ' + error.message);
        return;
    }
    
    try {
        let response;
        if (cardId) {
            // Update existing
            response = await fetch(`/api/entity_cards/${cardId}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(formData)
            });
        } else {
            // Create new
            response = await fetch('/api/entity_cards', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(formData)
            });
        }
        
        const data = await response.json();
        
        if (data.success) {
            closeEditModal();
            await refreshCardsView({ refreshTypes: !cardId });
        } else {
            alert('Error saving card: ' + data.message);
        }
    } catch (error) {
        console.error('Error saving card:', error);
        alert('Error saving card: ' + error.message);
    }
}

async function deleteCard(cardId) {
    if (!confirm('Are you sure you want to delete this entity card? This action cannot be undone.')) {
        return;
    }
    
    try {
        const response = await fetch(`/api/entity_cards/${cardId}`, {
            method: 'DELETE'
        });
        
        const data = await response.json();
        
        if (data.success) {
            // Immediate UI feedback: remove row before full refresh.
            const row = document.querySelector(`[data-card-id="${cardId}"]`);
            if (row) {
                row.remove();
            }
            if (currentCard && currentCard.id === cardId) {
                closeViewModal();
            }
            await refreshCardsView({ refreshTypes: true });
        } else {
            alert('Error deleting card: ' + data.message);
        }
    } catch (error) {
        console.error('Error deleting card:', error);
        alert('Error deleting card: ' + error.message);
    }
}

// ========================================================================
// SEARCH AND FILTERS
// ========================================================================

function searchCards() {
    currentFilters.search = document.getElementById('search-input').value;
    currentPage = 0;
    loadCards();
}

function clearFilters() {
    document.getElementById('search-input').value = '';
    document.getElementById('type-filter').value = '';
    document.getElementById('sort-select').value = 'name';
    currentFilters = { search: '', type: '', sort: 'name' };
    currentPage = 0;
    loadCards();
}

function changePage(delta) {
    currentPage += delta;
    if (currentPage < 0) currentPage = 0;
    loadCards();
}

// ========================================================================
// UTILITY FUNCTIONS
// ========================================================================

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function formatDate(dateString) {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    return date.toLocaleString();
}

