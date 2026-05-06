// ========================================================================
// GLOBAL STATE
// ========================================================================
let currentTab = 'tree';
let taxonomyTree = null;
let suggestions = [];
let reviews = [];

// ========================================================================
// INITIALIZATION
// ========================================================================
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    setupTreeSearch();
    loadAll();
});

// ========================================================================
// TAB MANAGEMENT
// ========================================================================
function initTabs() {
    const tabButtons = document.querySelectorAll('.tab-btn');
    tabButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            // Update active button
            tabButtons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            // Update active content
            const tabName = btn.getAttribute('data-tab');
            currentTab = tabName;
            
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });
            document.getElementById(`${tabName}-tab`).classList.add('active');
        });
    });
}

// ========================================================================
// DATA LOADING
// ========================================================================
async function loadAll() {
    console.log('loadAll() called - refreshing all data');
    await loadStatistics();
    await loadTaxonomyTree();
    await loadSuggestions();
    await loadReviews();
    console.log('loadAll() completed');
}

async function loadStatistics() {
    try {
        const response = await fetch('/api/taxonomy/statistics');
        const data = await response.json();
        
        if (data.success) {
            const stats = data.stats;
            document.getElementById('stat-total-types').textContent = stats.total_types;
            document.getElementById('stat-classified').textContent = stats.classified_nodes;
            document.getElementById('stat-unclassified').textContent = stats.unclassified_nodes;
            document.getElementById('stat-pending-suggestions').textContent = stats.pending_suggestions;
            document.getElementById('stat-pending-reviews').textContent = stats.pending_reviews;
            document.getElementById('stat-provisional').textContent = stats.provisional_classifications;
            
            document.getElementById('suggestions-count').textContent = stats.pending_suggestions;
            document.getElementById('reviews-count').textContent = stats.pending_reviews;
        }
    } catch (error) {
        console.error('Error loading statistics:', error);
    }
}

async function loadTaxonomyTree() {
    console.log('loadTaxonomyTree() called');
    try {
        const response = await fetch('/api/taxonomy/tree');
        const data = await response.json();
        console.log('Taxonomy tree data received:', data);
        
        if (data.success) {
            taxonomyTree = data.tree;
            window.taxonomyTreeData = data.tree;  // Store globally for path lookups
            console.log('Rendering taxonomy tree with', data.tree.length, 'root nodes');
            renderTaxonomyTree(taxonomyTree);
            console.log('Taxonomy tree rendered');
        } else {
            document.getElementById('taxonomy-tree').innerHTML = `
                <div class="message message-error">${data.message}</div>
            `;
        }
    } catch (error) {
        console.error('Error in loadTaxonomyTree:', error);
        document.getElementById('taxonomy-tree').innerHTML = `
            <div class="message message-error">Error loading taxonomy tree: ${error.message}</div>
        `;
    }
}

async function loadSuggestions() {
    try {
        const response = await fetch('/api/taxonomy/suggestions');
        const data = await response.json();
        
        if (data.success) {
            suggestions = data.suggestions;
            renderSuggestions(suggestions);
        } else {
            document.getElementById('suggestions-list').innerHTML = `
                <div class="message message-error">${data.message}</div>
            `;
        }
    } catch (error) {
        document.getElementById('suggestions-list').innerHTML = `
            <div class="message message-error">Error loading suggestions: ${error.message}</div>
        `;
    }
}

async function loadReviews() {
    try {
        const response = await fetch('/api/taxonomy/reviews');
        const data = await response.json();
        
        if (data.success) {
            reviews = data.reviews;
            renderReviews(reviews);
        } else {
            document.getElementById('reviews-list').innerHTML = `
                <div class="message message-error">${data.message}</div>
            `;
        }
    } catch (error) {
        document.getElementById('reviews-list').innerHTML = `
            <div class="message message-error">Error loading reviews: ${error.message}</div>
        `;
    }
}

// ========================================================================
// TAXONOMY TREE RENDERING
// ========================================================================
function renderTaxonomyTree(tree) {
    const container = document.getElementById('taxonomy-tree');
    container.innerHTML = '';
    
    if (tree.length === 0) {
        container.innerHTML = '<div class="message message-info">No taxonomy types found</div>';
        return;
    }
    
    tree.forEach(node => {
        container.appendChild(createTreeNode(node));
    });
}

function createTreeNode(node) {
    const div = document.createElement('div');
    div.className = 'tree-node';
    
    const hasChildren = node.children && node.children.length > 0;
    
    const label = document.createElement('div');
    label.className = 'tree-node-label';
    
    if (hasChildren) {
        const toggle = document.createElement('span');
        toggle.className = 'tree-toggle collapsed';
        toggle.addEventListener('click', function(e) {
            e.stopPropagation();
            this.classList.toggle('collapsed');
            this.classList.toggle('expanded');
            // Find children div at click time
            const childrenDiv = this.parentElement.parentElement.querySelector('.tree-children');
            if (childrenDiv) {
                childrenDiv.classList.toggle('expanded');
            }
        });
        label.appendChild(toggle);
    } else {
        const spacer = document.createElement('span');
        spacer.style.width = '20px';
        spacer.style.display = 'inline-block';
        label.appendChild(spacer);
    }
    
    const labelText = document.createElement('span');
    labelText.textContent = node.label;
    labelText.style.fontWeight = '600';
    label.appendChild(labelText);
    
    if (node.usage_count > 0) {
        const badge = document.createElement('span');
        badge.className = 'usage-badge';
        badge.textContent = `${node.usage_count} nodes`;
        label.appendChild(badge);
    }
    
    // Add action buttons
    const actions = document.createElement('span');
    actions.className = 'tree-actions';
    actions.style.marginLeft = '10px';
    actions.style.opacity = '0';
    actions.style.transition = 'opacity 0.2s';
    
    const editBtn = document.createElement('button');
    editBtn.textContent = '✏️ Edit';
    editBtn.className = 'btn-secondary';
    editBtn.style.fontSize = '11px';
    editBtn.style.padding = '2px 6px';
    editBtn.style.marginRight = '4px';
    editBtn.onclick = (e) => {
        e.stopPropagation();
        openEditModal(node);
    };
    actions.appendChild(editBtn);
    
    const addBtn = document.createElement('button');
    addBtn.textContent = '➕ Add Child';
    addBtn.className = 'btn-secondary';
    addBtn.style.fontSize = '11px';
    addBtn.style.padding = '2px 6px';
    addBtn.onclick = (e) => {
        e.stopPropagation();
        openAddChildModal(node);
    };
    actions.appendChild(addBtn);
    
    label.appendChild(actions);
    
    // Show actions on hover
    label.onmouseenter = () => { actions.style.opacity = '1'; };
    label.onmouseleave = () => { actions.style.opacity = '0'; };
    
    div.appendChild(label);
    
    if (hasChildren) {
        const children = document.createElement('div');
        children.className = 'tree-children';
        node.children.forEach(child => {
            children.appendChild(createTreeNode(child));
        });
        div.appendChild(children);
    }
    
    return div;
}

// ========================================================================
// TAXONOMY TREE SEARCH
// ========================================================================
let searchTimeout = null;

function setupTreeSearch() {
    const searchInput = document.getElementById('tree-search');
    const searchResults = document.getElementById('search-results');
    
    searchInput.addEventListener('input', () => {
        clearTimeout(searchTimeout);
        searchTimeout = setTimeout(() => {
            const query = searchInput.value.trim().toLowerCase();
            if (query.length === 0) {
                clearSearch();
                searchResults.textContent = '';
            } else {
                performSearch(query);
            }
        }, 300); // Debounce 300ms
    });
    
    // Clear search on ESC key
    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            searchInput.value = '';
            clearSearch();
            searchResults.textContent = '';
        }
    });
}

function performSearch(query) {
    const allNodes = document.querySelectorAll('.tree-node');
    let matchCount = 0;
    let matchedNodes = [];
    
    // First pass: find matches and hide non-matches
    allNodes.forEach(node => {
        const label = node.querySelector('.tree-node-label span')?.textContent?.toLowerCase() || '';
        const isMatch = label.includes(query);
        
        if (isMatch) {
            matchCount++;
            matchedNodes.push(node);
            node.style.display = '';
            // Highlight the match
            const labelSpan = node.querySelector('.tree-node-label span');
            if (labelSpan) {
                const text = labelSpan.textContent;
                const index = text.toLowerCase().indexOf(query);
                if (index !== -1) {
                    const before = text.substring(0, index);
                    const match = text.substring(index, index + query.length);
                    const after = text.substring(index + query.length);
                    labelSpan.innerHTML = `${before}<span style="background-color: yellow; font-weight: bold;">${match}</span>${after}`;
                }
            }
        } else {
            node.style.display = 'none';
        }
    });
    
    // Second pass: show parent nodes of matches and expand them
    matchedNodes.forEach(node => {
        let parent = node.parentElement;
        while (parent) {
            if (parent.classList.contains('tree-node')) {
                parent.style.display = '';
            }
            if (parent.classList.contains('tree-children')) {
                parent.classList.add('expanded');
                // Find and expand the toggle
                const parentNode = parent.previousElementSibling;
                if (parentNode) {
                    const toggle = parentNode.querySelector('.tree-toggle');
                    if (toggle) {
                        toggle.classList.remove('collapsed');
                        toggle.classList.add('expanded');
                    }
                }
            }
            parent = parent.parentElement;
        }
    });
    
    // Update results
    const searchResults = document.getElementById('search-results');
    if (matchCount === 0) {
        searchResults.innerHTML = '<span style="color: #e74c3c;">❌ No matches found</span>';
    } else {
        searchResults.innerHTML = `<span style="color: #27ae60;">✅ Found ${matchCount} match${matchCount !== 1 ? 'es' : ''}</span>`;
    }
}

function clearSearch() {
    // Show all nodes
    const allNodes = document.querySelectorAll('.tree-node');
    allNodes.forEach(node => {
        node.style.display = '';
    });
    
    // Remove highlights
    const allLabels = document.querySelectorAll('.tree-node-label span');
    allLabels.forEach(label => {
        // Restore plain text (remove HTML highlights)
        if (label.innerHTML.includes('<span')) {
            label.textContent = label.textContent;
        }
    });
    
    // Collapse all
    const allChildren = document.querySelectorAll('.tree-children');
    allChildren.forEach(children => {
        children.classList.remove('expanded');
    });
    
    const allToggles = document.querySelectorAll('.tree-toggle');
    allToggles.forEach(toggle => {
        toggle.classList.add('collapsed');
        toggle.classList.remove('expanded');
    });
}

// ========================================================================
// SUGGESTIONS RENDERING
// ========================================================================
function renderSuggestions(suggestions) {
    const container = document.getElementById('suggestions-list');
    container.innerHTML = '';
    
    if (suggestions.length === 0) {
        container.innerHTML = '<div class="message message-info">No pending suggestions</div>';
        return;
    }
    
    // Add "Accept All" button at the top if there are suggestions
    if (suggestions.length > 0) {
        const headerDiv = document.createElement('div');
        headerDiv.style.cssText = 'display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; padding: 10px; background: #ecf0f1; border-radius: 5px;';
        headerDiv.innerHTML = `
            <div style="font-weight: bold; color: #2c3e50;">
                ${suggestions.length} Pending Suggestion${suggestions.length > 1 ? 's' : ''}
            </div>
            <button class="btn btn-success" onclick="approveAllSuggestions()" style="font-weight: bold;">
                ✓ Accept All (${suggestions.length})
            </button>
        `;
        container.appendChild(headerDiv);
    }
    
    suggestions.forEach(suggestion => {
        container.appendChild(createSuggestionCard(suggestion));
    });
}

function createSuggestionCard(suggestion) {
    const card = document.createElement('div');
    card.className = 'review-card';
    card.id = `suggestion-${suggestion.id}`;
    
    card.innerHTML = `
        <div class="review-header">
            <div class="review-title">New Type: ${suggestion.suggested_label}</div>
            <span class="review-badge badge-suggestion">Suggestion</span>
        </div>
        <div class="review-details">
            <div class="detail-row">
                <div class="detail-label">Parent Path:</div>
                <div class="detail-value path-display">${suggestion.parent_path}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">Full Path:</div>
                <div class="detail-value path-display">${suggestion.parent_path} &gt; ${suggestion.suggested_label}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">Confidence:</div>
                <div class="detail-value">
                    ${(suggestion.confidence * 100).toFixed(0)}%
                    <div class="confidence-bar">
                        <div class="confidence-fill ${getConfidenceClass(suggestion.confidence)}" 
                             style="width: ${suggestion.confidence * 100}%"></div>
                    </div>
                </div>
            </div>
            ${suggestion.reasoning ? `
                <div class="detail-row">
                    <div class="detail-label">Reasoning:</div>
                    <div class="detail-value">${suggestion.reasoning}</div>
                </div>
            ` : ''}
            ${suggestion.example_nodes && suggestion.example_nodes.length > 0 ? `
                <div class="detail-row">
                    <div class="detail-label">Example Nodes:</div>
                    <div class="detail-value">
                        <div class="example-nodes">
                            ${suggestion.example_nodes.map(node => `
                                <div class="example-node">
                                    <strong>${node.label}</strong> (${node.node_type})<br>
                                    <small>${node.sentence}</small>
                                </div>
                            `).join('')}
                        </div>
                    </div>
                </div>
            ` : ''}
        </div>
        <div class="review-actions">
            <button class="btn btn-success" onclick="approveSuggestion(${suggestion.id})">
                ✓ Approve
            </button>
            <button class="btn btn-primary" onclick="openEditSuggestionModal(${suggestion.id}, '${suggestion.suggested_label.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}', '${suggestion.parent_path.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">
                ✎ Edit
            </button>
            <button class="btn btn-danger" onclick="rejectSuggestion(${suggestion.id})">
                ✗ Reject
            </button>
        </div>
    `;
    
    return card;
}

// ========================================================================
// REVIEWS RENDERING
// ========================================================================
function renderReviews(reviews) {
    const container = document.getElementById('reviews-list');
    container.innerHTML = '';
    
    if (reviews.length === 0) {
        container.innerHTML = '<div class="message message-info">No pending reviews</div>';
        return;
    }
    
    reviews.forEach(review => {
        container.appendChild(createReviewCard(review));
    });
}

function createReviewCard(review) {
    const card = document.createElement('div');
    card.className = 'review-card';
    card.id = `review-${review.id}`;
    
    const isLowConfidence = review.confidence < 0.7;
    
    card.innerHTML = `
        <div class="review-header">
            <div class="review-title">${review.node_label}</div>
            <span class="review-badge ${isLowConfidence ? 'badge-low-confidence' : 'badge-review'}">
                ${review.action}
            </span>
        </div>
        <div class="review-details">
            <div class="detail-row">
                <div class="detail-label">Label:</div>
                <div class="detail-value"><strong>${review.node_label}</strong></div>
            </div>
            ${review.node_semantic_label ? `
                <div class="detail-row">
                    <div class="detail-label">Semantic Label:</div>
                    <div class="detail-value">${review.node_semantic_label}</div>
                </div>
            ` : ''}
            <div class="detail-row">
                <div class="detail-label">Node Type:</div>
                <div class="detail-value">${review.node_type}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">Sentence:</div>
                <div class="detail-value">${review.node_sentence || 'N/A'}</div>
            </div>
            ${review.proposed_path ? `
                <div class="detail-row">
                    <div class="detail-label">Proposed Path:</div>
                    <div class="detail-value path-display">${review.proposed_path}</div>
                </div>
            ` : ''}
            <div class="detail-row">
                <div class="detail-label">Validated Path:</div>
                <div class="detail-value path-display">${review.validated_path}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">Confidence:</div>
                <div class="detail-value">
                    ${(review.confidence * 100).toFixed(0)}%
                    <div class="confidence-bar">
                        <div class="confidence-fill ${getConfidenceClass(review.confidence)}" 
                             style="width: ${review.confidence * 100}%"></div>
                    </div>
                </div>
            </div>
            ${review.reasoning ? `
                <div class="detail-row">
                    <div class="detail-label">Reasoning:</div>
                    <div class="detail-value">${review.reasoning}</div>
                </div>
            ` : ''}
        </div>
        <div class="review-actions">
            <button class="btn btn-success" onclick="approveReview(${review.id})">
                ✓ Approve
            </button>
            <button class="btn btn-primary" onclick="openEditReviewModal(${review.id}, '${review.node_label.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}', '${review.validated_path.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}')">
                ✎ Edit
            </button>
            <button class="btn btn-danger" onclick="rejectReview(${review.id})" title="Don't apply any taxonomy, put node back in queue">
                ✗ Reject Both
            </button>
            ${review.proposed_path ? `
                <button class="btn btn-warning" onclick="acceptProposer(${review.id})" 
                        style="font-size: 11px; padding: 6px 10px; margin-left: 10px;" 
                        title="Bypass critic, accept proposer's original path">
                    ↺ Accept Proposer
                </button>
            ` : ''}
        </div>
    `;
    
    return card;
}

// ========================================================================
// ACTIONS - SUGGESTIONS
// ========================================================================
async function approveAllSuggestions() {
    const count = document.querySelectorAll('[id^="suggestion-"]').length;
    if (!confirm(`Are you sure you want to approve ALL ${count} pending suggestions?\n\nThis will create new taxonomy categories for all of them.`)) return;
    
    try {
        showMessage('info', `Approving ${count} suggestions...`);
        
        const response = await fetch('/api/taxonomy/suggestions/approve_all', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', `✓ Successfully approved ${data.approved_count} suggestions!`);
            await loadAll();
        } else {
            showMessage('error', data.message || 'Failed to approve all suggestions');
        }
    } catch (error) {
        console.error('Error approving all suggestions:', error);
        showMessage('error', 'Error approving all suggestions');
    }
}

async function approveSuggestion(suggestionId) {
    if (!confirm('Approve this taxonomy suggestion?')) return;
    
    try {
        const response = await fetch(`/api/taxonomy/suggestions/${suggestionId}/approve`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            document.getElementById(`suggestion-${suggestionId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

function openEditSuggestionModal(suggestionId, currentLabel, parentPath) {
    document.getElementById('suggestionId').value = suggestionId;
    document.getElementById('suggestionLabel').value = currentLabel;
    document.getElementById('suggestionParentPath').value = parentPath;  // Now editable!
    document.getElementById('editSuggestionModal').style.display = 'block';
}

function closeEditSuggestionModal() {
    document.getElementById('editSuggestionModal').style.display = 'none';
}

async function saveSuggestion() {
    const suggestionId = document.getElementById('suggestionId').value;
    const newLabel = document.getElementById('suggestionLabel').value.trim();
    const newParentPath = document.getElementById('suggestionParentPath').value.trim();
    
    if (!newLabel) {
        showMessage('error', 'Label cannot be empty');
        return;
    }
    
    if (!newParentPath) {
        showMessage('error', 'Parent path cannot be empty');
        return;
    }
    
    try {
        const response = await fetch(`/api/taxonomy/suggestions/${suggestionId}/approve`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                new_label: newLabel,
                new_parent_path: newParentPath
            })
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            closeEditSuggestionModal();
            document.getElementById(`suggestion-${suggestionId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

async function rejectSuggestion(suggestionId) {
    if (!confirm('Reject this taxonomy suggestion?')) return;
    
    try {
        const response = await fetch(`/api/taxonomy/suggestions/${suggestionId}/reject`, {
            method: 'POST'
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            document.getElementById(`suggestion-${suggestionId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

// ========================================================================
// ACTIONS - REVIEWS
// ========================================================================
async function approveReview(reviewId) {
    if (!confirm('Approve this node classification?')) return;
    
    try {
        const response = await fetch(`/api/taxonomy/reviews/${reviewId}/approve`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            document.getElementById(`review-${reviewId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

function openEditReviewModal(reviewId, nodeLabel, currentPath) {
    document.getElementById('reviewId').value = reviewId;
    document.getElementById('reviewNodeLabel').textContent = nodeLabel;
    document.getElementById('reviewPath').value = currentPath;
    document.getElementById('editReviewModal').style.display = 'block';
}

function closeEditReviewModal() {
    document.getElementById('editReviewModal').style.display = 'none';
}

async function saveReview() {
    const reviewId = document.getElementById('reviewId').value;
    const newPath = document.getElementById('reviewPath').value.trim();
    
    if (!newPath) {
        showMessage('error', 'Taxonomy path cannot be empty');
        return;
    }
    
    try {
        const response = await fetch(`/api/taxonomy/reviews/${reviewId}/approve`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ final_path: newPath })
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            closeEditReviewModal();
            document.getElementById(`review-${reviewId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

async function rejectReview(reviewId) {
    if (!confirm('Reject BOTH proposer and critic? This will put the node back in the unclassified queue for re-classification.')) return;
    
    try {
        const response = await fetch(`/api/taxonomy/reviews/${reviewId}/reject`, {
            method: 'POST'
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            document.getElementById(`review-${reviewId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

async function acceptProposer(reviewId) {
    if (!confirm("Accept the proposer's original suggestion, bypassing the critic?")) return;
    
    try {
        const response = await fetch(`/api/taxonomy/reviews/${reviewId}/accept-proposer`, {
            method: 'POST'
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            document.getElementById(`review-${reviewId}`).remove();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

// ========================================================================
// UTILITIES
// ========================================================================
function getConfidenceClass(confidence) {
    if (confidence >= 0.8) return 'confidence-high';
    if (confidence >= 0.6) return 'confidence-medium';
    return 'confidence-low';
}

function showMessage(type, text) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message message-${type}`;
    messageDiv.textContent = text;
    
    const container = document.querySelector('.container');
    container.insertBefore(messageDiv, container.firstChild);
    
    setTimeout(() => {
        messageDiv.remove();
    }, 5000);
}

// ========================================================================
// TAXONOMY EDIT/ADD MODALS
// ========================================================================
async function openEditModal(node) {
    const modal = document.getElementById('editModal');
    
    // Set values
    document.getElementById('editNodeId').value = node.id;
    document.getElementById('editNodeIdDisplay').textContent = node.id;
    document.getElementById('editLabel').value = node.label;
    document.getElementById('editDescription').value = node.description || '';
    document.getElementById('editCurrentParentId').value = node.parent_id || '';
    document.getElementById('editPath').textContent = getNodePath(node);
    
    // Show current parent info
    if (node.parent_id) {
        const parent = findNodeById(window.taxonomyTreeData, node.parent_id);
        if (parent) {
            document.getElementById('editCurrentParentDisplay').textContent = 
                `${parent.label} (ID: ${parent.id})`;
        } else {
            document.getElementById('editCurrentParentDisplay').textContent = 
                `(ID: ${node.parent_id}) - not found in tree`;
        }
        // Pre-fill the new parent ID field with current parent
        document.getElementById('editParent').value = node.parent_id;
    } else {
        document.getElementById('editCurrentParentDisplay').textContent = '(None - Root Category)';
        document.getElementById('editParent').value = '';
    }
    
    modal.style.display = 'block';
}

async function populateParentDropdown(selectId, currentNodeId, currentParentId) {
    const select = document.getElementById(selectId);
    select.innerHTML = '<option value="">-- Root Category (No Parent) --</option>';
    
    try {
        const response = await fetch('/api/taxonomy/tree');
        const data = await response.json();
        
        if (data.success) {
            // Flatten the tree to get all nodes
            const allNodes = [];
            function collectNodes(nodes) {
                nodes.forEach(node => {
                    allNodes.push(node);
                    if (node.children && node.children.length > 0) {
                        collectNodes(node.children);
                    }
                });
            }
            collectNodes(data.tree);
            
            // Sort alphabetically by path
            allNodes.sort((a, b) => {
                const pathA = getFullPath(a, allNodes).join(' > ');
                const pathB = getFullPath(b, allNodes).join(' > ');
                return pathA.localeCompare(pathB);
            });
            
            // Add options (exclude current node and its descendants)
            allNodes.forEach(node => {
                // Don't allow setting self or descendants as parent
                if (node.id !== currentNodeId && !isDescendantOf(node.id, currentNodeId, allNodes)) {
                    const path = getFullPath(node, allNodes).join(' > ');
                    const option = document.createElement('option');
                    option.value = node.id;
                    option.textContent = path;
                    if (node.id === currentParentId) {
                        option.selected = true;
                    }
                    select.appendChild(option);
                }
            });
        }
    } catch (error) {
        console.error('Error loading parent options:', error);
    }
}

function getFullPath(node, allNodes) {
    const path = [node.label];
    let current = node;
    while (current.parent_id) {
        const parent = allNodes.find(n => n.id === current.parent_id);
        if (parent) {
            path.unshift(parent.label);
            current = parent;
        } else {
            break;
        }
    }
    return path;
}

function isDescendantOf(nodeId, ancestorId, allNodes) {
    const node = allNodes.find(n => n.id === nodeId);
    if (!node || !node.parent_id) return false;
    if (node.parent_id === ancestorId) return true;
    return isDescendantOf(node.parent_id, ancestorId, allNodes);
}

function openAddChildModal(node) {
    const modal = document.getElementById('addChildModal');
    document.getElementById('parentNodeId').value = node.id;
    document.getElementById('parentPath').textContent = getNodePath(node);
    document.getElementById('newChildLabel').value = '';
    modal.style.display = 'block';
}

function closeEditModal() {
    document.getElementById('editModal').style.display = 'none';
}

function closeAddChildModal() {
    document.getElementById('addChildModal').style.display = 'none';
}

function getNodePath(node) {
    // Build full path by traversing up to root
    const path = [node.label];
    let current = node;
    
    // Try to find parent in the tree data
    while (current.parent_id) {
        // We need to search the tree for the parent
        // This is inefficient but works for now
        const parent = findNodeById(window.taxonomyTreeData, current.parent_id);
        if (parent) {
            path.unshift(parent.label);
            current = parent;
        } else {
            break;
        }
    }
    
    return path.join(' > ');
}

function findNodeById(nodes, targetId) {
    for (const node of nodes) {
        if (node.id === targetId) {
            return node;
        }
        if (node.children && node.children.length > 0) {
            const found = findNodeById(node.children, targetId);
            if (found) return found;
        }
    }
    return null;
}

async function submitEdit() {
    const nodeId = document.getElementById('editNodeId').value;
    const newLabel = document.getElementById('editLabel').value.trim();
    const newDescription = document.getElementById('editDescription').value.trim();
    const newParentIdStr = document.getElementById('editParent').value.trim();
    const currentParentId = document.getElementById('editCurrentParentId').value || null;
    
    if (!newLabel) {
        showMessage('error', 'Label cannot be empty');
        return;
    }
    
    // Convert newParentId to number or null
    let newParentId = null;
    if (newParentIdStr !== '') {
        newParentId = parseInt(newParentIdStr, 10);
        if (isNaN(newParentId)) {
            showMessage('error', 'Parent ID must be a valid number');
            return;
        }
    }
    
    const payload = { 
        new_label: newLabel,
        new_description: newDescription  // Always include description
    };
    
    // Only include parent_id if it changed
    const currentParentIdNum = currentParentId ? parseInt(currentParentId, 10) : null;
    if (newParentId !== currentParentIdNum) {
        payload.new_parent_id = newParentId;
    }
    
    try {
        const response = await fetch(`/api/taxonomy/edit/${nodeId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            closeEditModal();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

async function deleteCategory() {
    const nodeId = document.getElementById('editNodeId').value;
    const label = document.getElementById('editLabel').value.trim();
    const path = document.getElementById('editPath').textContent;
    
    const confirmed = confirm(
        `⚠️ WARNING: Delete "${path}"?\n\n` +
        `This will permanently delete this category and ALL its children.\n` +
        `Any nodes classified with these categories will become unclassified.\n\n` +
        `This action CANNOT be undone!`
    );
    
    if (!confirmed) {
        return;
    }
    
    try {
        const response = await fetch(`/api/taxonomy/delete/${nodeId}`, {
            method: 'DELETE'
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            closeEditModal();
            await loadAll();
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

async function submitAddChild() {
    const parentId = document.getElementById('parentNodeId').value;
    const childLabel = document.getElementById('newChildLabel').value.trim();
    
    console.log('submitAddChild called', { parentId, childLabel });
    
    if (!childLabel) {
        showMessage('error', 'Child label cannot be empty');
        return;
    }
    
    try {
        console.log('Making API call to /api/taxonomy/add');
        const response = await fetch('/api/taxonomy/add', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ 
                parent_id: parseInt(parentId), 
                label: childLabel 
            })
        });
        
        console.log('Response status:', response.status);
        const data = await response.json();
        console.log('Response data:', data);
        
        if (data.success) {
            showMessage('success', data.message);
            console.log('Closing modal and reloading...');
            closeAddChildModal();
            await loadAll();
        } else {
            console.error('API returned error:', data.message);
            showMessage('error', data.message);
        }
    } catch (error) {
        console.error('Exception in submitAddChild:', error);
        showMessage('error', `Error: ${error.message}`);
    }
}

// ========================================================================
// NODE SEARCH
// ========================================================================
async function searchNodes() {
    const query = document.getElementById('node-search-input').value.trim();
    const resultsDiv = document.getElementById('node-search-results');
    
    if (!query) {
        showMessage('error', 'Please enter a search term');
        return;
    }
    
    resultsDiv.innerHTML = '<div class="loading"><div class="spinner"></div><p>Searching...</p></div>';
    
    try {
        const response = await fetch(`/api/taxonomy/search-nodes?query=${encodeURIComponent(query)}`);
        const data = await response.json();
        
        if (data.success) {
            displayNodeSearchResults(data.nodes);
        } else {
            resultsDiv.innerHTML = `<div class="message message-error">${data.message}</div>`;
        }
    } catch (error) {
        resultsDiv.innerHTML = `<div class="message message-error">Error: ${error.message}</div>`;
    }
}

function displayNodeSearchResults(nodes) {
    const resultsDiv = document.getElementById('node-search-results');
    
    if (nodes.length === 0) {
        resultsDiv.innerHTML = '<div class="message message-info">No nodes found</div>';
        return;
    }
    
    resultsDiv.innerHTML = `<div style="margin-bottom: 10px; color: #27ae60; font-weight: bold;">✅ Found ${nodes.length} node${nodes.length !== 1 ? 's' : ''}</div>`;
    
    nodes.forEach(node => {
        const card = document.createElement('div');
        card.className = 'review-card';
        card.style.marginBottom = '15px';
        
        const taxonomiesHtml = node.taxonomies && node.taxonomies.length > 0
            ? node.taxonomies.map(tax => `
                <div style="display: flex; justify-content: space-between; align-items: center; padding: 8px; background: #ecf0f1; border-radius: 4px; margin-top: 5px;">
                    <span>${tax.path}</span>
                    <div style="display: flex; gap: 5px;">
                        <button onclick="openEditNodeTaxonomyModal('${node.id}', ${tax.taxonomy_id}, '${tax.path.replace(/'/g, "\\'")}', '${node.label.replace(/'/g, "\\'")}' )" class="btn-primary" style="font-size: 11px; padding: 4px 8px;">
                            ✎ Edit
                        </button>
                        <button onclick="removeNodeTaxonomy('${node.id}', ${tax.taxonomy_id})" class="btn-danger" style="font-size: 11px; padding: 4px 8px;">
                            ✗ Remove
                        </button>
                    </div>
                </div>
            `).join('')
            : '<div style="color: #95a5a6; font-style: italic;">No taxonomy classifications</div>';
        
        card.innerHTML = `
            <div class="review-header">
                <div class="review-title">${node.label}</div>
                <span class="review-badge badge-review">${node.node_type}</span>
            </div>
            <div class="review-details">
                <div class="detail-row">
                    <div class="detail-label">Node ID:</div>
                    <div class="detail-value" style="font-family: monospace; font-size: 11px;">${node.id}</div>
                </div>
                ${node.semantic_label ? `
                    <div class="detail-row">
                        <div class="detail-label">Semantic Label:</div>
                        <div class="detail-value">${node.semantic_label}</div>
                    </div>
                ` : ''}
                <div class="detail-row">
                    <div class="detail-label">Current Taxonomies:</div>
                    <div class="detail-value">
                        ${taxonomiesHtml}
                    </div>
                </div>
            </div>
        `;
        
        resultsDiv.appendChild(card);
    });
}

async function removeNodeTaxonomy(nodeId, taxonomyId) {
    if (!confirm('Remove this taxonomy classification from the node?')) return;
    
    try {
        const response = await fetch(`/api/taxonomy/node/${nodeId}/taxonomy/${taxonomyId}`, {
            method: 'DELETE'
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            searchNodes(); // Refresh search results
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

function openEditNodeTaxonomyModal(nodeId, oldTaxonomyId, currentPath, nodeLabel) {
    document.getElementById('editNodeTaxonomyNodeId').value = nodeId;
    document.getElementById('editNodeTaxonomyOldId').value = oldTaxonomyId;
    document.getElementById('editNodeTaxonomyPath').value = currentPath;
    document.getElementById('editNodeTaxonomyNodeLabel').textContent = nodeLabel;
    document.getElementById('editNodeTaxonomyModal').style.display = 'block';
}

function closeEditNodeTaxonomyModal() {
    document.getElementById('editNodeTaxonomyModal').style.display = 'none';
}

async function saveNodeTaxonomy() {
    const nodeId = document.getElementById('editNodeTaxonomyNodeId').value;
    const oldTaxonomyId = document.getElementById('editNodeTaxonomyOldId').value;
    const newPath = document.getElementById('editNodeTaxonomyPath').value.trim();
    
    if (!newPath) {
        showMessage('error', 'Taxonomy path cannot be empty');
        return;
    }
    
    try {
        const response = await fetch(`/api/taxonomy/node/${nodeId}/taxonomy/${oldTaxonomyId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ new_path: newPath })
        });
        const data = await response.json();
        
        if (data.success) {
            showMessage('success', data.message);
            closeEditNodeTaxonomyModal();
            searchNodes(); // Refresh search results
        } else {
            showMessage('error', data.message);
        }
    } catch (error) {
        showMessage('error', `Error: ${error.message}`);
    }
}

// ========================================================================
// TAXONOMY EXPORT
// ========================================================================
async function exportTaxonomy(format = 'tree') {
    try {
        const response = await fetch(`/api/taxonomy/export?format=${format}`);
        const data = await response.json();
        
        if (!response.ok) {
            showMessage('error', `Export failed: ${data.message || 'Unknown error'}`);
            return;
        }
        
        // Create a downloadable JSON file
        const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
        const filename = `taxonomy_export_${format}_${timestamp}.json`;
        
        const jsonStr = JSON.stringify(data, null, 2);
        const blob = new Blob([jsonStr], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        
        // Create a temporary link and trigger download
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        
        // Cleanup
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        
        showMessage('success', `Exported ${data.metadata?.total_nodes || data.nodes?.length || Object.keys(data.paths || {}).length} taxonomy nodes to ${filename}`);
        
    } catch (error) {
        console.error('Export error:', error);
        showMessage('error', `Export failed: ${error.message}`);
    }
}

// Note: Modals only close via Cancel or Save buttons, not by clicking outside
