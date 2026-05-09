// Features Settings
const features = ['email', 'calendar', 'tasks', 'scheduler', 'weather', 'news', 'kg', 'entity_cards'];

document.addEventListener('DOMContentLoaded', function() {
    loadFeatureSettings();

    features.forEach(feature => {
        const checkbox = document.getElementById(`enable_${feature}`);
        if (checkbox) {
            checkbox.addEventListener('change', function() {
                toggleFeature(feature, this.checked);
            });
        }
    });
});

async function loadFeatureSettings() {
    try {
        // Load feature settings
        const response = await fetch('/api/settings/features');
        const data = await response.json();
        
        if (data.success && data.features) {
            features.forEach(feature => {
                const checkbox = document.getElementById(`enable_${feature}`);
                const statusEl = document.getElementById(`status_${feature}`);
                
                if (checkbox) {
                    const isEnabled = data.features[`enable_${feature}`];
                    checkbox.checked = isEnabled;
                    updateFeatureStatus(feature, isEnabled);
                }
            });
        }
        
        // Check API key dependencies
        checkFeatureAvailability();
    } catch (error) {
        console.error('Error loading feature settings:', error);
        showError('Failed to load feature settings');
    }
}

async function checkFeatureAvailability() {
    try {
        const response = await fetch('/api/settings/features/availability');
        const data = await response.json();
        
        if (data.success && data.features) {
            features.forEach(feature => {
                const availability = data.features[feature];
                const checkbox = document.getElementById(`enable_${feature}`);
                const statusEl = document.getElementById(`status_${feature}`);
                const featureCard = checkbox?.closest('.feature-card');
                
                if (availability) {
                    if (!availability.can_enable) {
                        // Feature cannot be enabled - gray it out
                        if (checkbox) {
                            checkbox.disabled = true;
                            checkbox.checked = false; // Auto-disable
                        }
                        if (featureCard) {
                            featureCard.classList.add('feature-unavailable');
                            
                            // Add OAuth button for Google services
                            if (['email', 'calendar', 'tasks'].includes(feature) && 
                                availability.missing_oauth && availability.missing_oauth.includes('google')) {
                                addOAuthButton(featureCard, feature);
                            }
                        }
                        if (statusEl) {
                            statusEl.innerHTML = `<i class="fas fa-lock"></i> ${availability.reason}`;
                            statusEl.className = 'feature-status unavailable';
                        }
                    } else if (checkbox && checkbox.checked && !availability.can_enable) {
                        // Feature is enabled but missing requirements (shouldn't happen after auto-disable)
                        if (statusEl) {
                            statusEl.innerHTML = `<i class="fas fa-exclamation-triangle"></i> ${availability.reason}`;
                            statusEl.className = 'feature-status warning';
                        }
                    }
                }
            });
        }
    } catch (error) {
        console.error('Error checking feature availability:', error);
    }
}

function addOAuthButton(featureCard, feature) {
    if (featureCard.querySelector('.oauth-connect-btn')) return;

    const buttonHtml = `
        <a href="/settings/integrations" class="oauth-connect-btn" style="display:inline-block;">
            <i class="fab fa-google"></i> Connect Google Account
        </a>
    `;
    const featureInfo = featureCard.querySelector('.feature-info');
    if (featureInfo) featureInfo.insertAdjacentHTML('beforeend', buttonHtml);
}

async function toggleFeature(feature, enabled) {
    const checkbox = document.getElementById(`enable_${feature}`);
    const statusEl = document.getElementById(`status_${feature}`);
    
    // Show loading state
    statusEl.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Updating...';
    statusEl.className = 'feature-status loading';
    
    try {
        const response = await fetch(`/api/settings/features/${feature}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled })
        });
        
        const result = await response.json();
        
        if (result.success) {
            updateFeatureStatus(feature, enabled);
            showSuccess(`${feature.charAt(0).toUpperCase() + feature.slice(1)} ${enabled ? 'enabled' : 'disabled'} successfully!`);
        } else {
            throw new Error(result.message || 'Failed to update feature');
        }
    } catch (error) {
        console.error('Error toggling feature:', error);
        showError('Failed to update feature: ' + error.message);
        // Revert checkbox state
        checkbox.checked = !enabled;
        updateFeatureStatus(feature, !enabled);
    }
}

function updateFeatureStatus(feature, enabled) {
    const statusEl = document.getElementById(`status_${feature}`);
    if (statusEl) {
        if (enabled) {
            statusEl.innerHTML = '<i class="fas fa-check-circle"></i> Enabled';
            statusEl.className = 'feature-status enabled';
        } else {
            statusEl.innerHTML = '<i class="fas fa-times-circle"></i> Disabled';
            statusEl.className = 'feature-status disabled';
        }
    }
}

function showError(message) {
    const errorDiv = document.getElementById('error-message');
    errorDiv.textContent = message;
    errorDiv.style.display = 'block';
    
    setTimeout(() => {
        errorDiv.style.display = 'none';
    }, 5000);
}

function showSuccess(message) {
    const successDiv = document.getElementById('success-message');
    successDiv.textContent = message;
    successDiv.style.display = 'block';
    
    setTimeout(() => {
        successDiv.style.display = 'none';
    }, 3000);
}

function escapeHtml(value) {
    return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function escapeHtmlAttr(value) {
    return escapeHtml(value).replaceAll('`', '&#96;');
}


