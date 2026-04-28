// Quiet Mode Settings
const features = ['email', 'calendar', 'tasks', 'scheduler', 'weather', 'news', 'kg', 'taxonomy', 'entity_cards'];

document.addEventListener('DOMContentLoaded', function() {
    loadQuietModeSettings();
    
    // Global quiet mode toggle
    const globalToggle = document.getElementById('enable_quiet_mode');
    if (globalToggle) {
        globalToggle.addEventListener('change', function() {
            toggleGlobalQuietMode(this.checked);
        });
    }
    
    // Universal time inputs
    document.getElementById('universal_start').addEventListener('change', saveUniversalHours);
    document.getElementById('universal_end').addEventListener('change', saveUniversalHours);
    
    // Per-feature toggles and time inputs
    features.forEach(feature => {
        const checkbox = document.getElementById(`enable_${feature}_quiet`);
        if (checkbox) {
            checkbox.addEventListener('change', function() {
                toggleFeatureQuietHours(feature, this.checked);
            });
        }
        
        // Time input listeners
        document.getElementById(`${feature}_start`).addEventListener('change', () => {
            saveFeatureHours(feature);
        });
        document.getElementById(`${feature}_end`).addEventListener('change', () => {
            saveFeatureHours(feature);
        });
    });
});

async function loadQuietModeSettings() {
    try {
        const response = await fetch('/api/settings/quiet-mode');
        const data = await response.json();
        
        if (data.success && data.quiet_mode) {
            const qm = data.quiet_mode;
            
            // Set global toggle
            const globalToggle = document.getElementById('enable_quiet_mode');
            globalToggle.checked = qm.enabled;
            updateGlobalStatus(qm.enabled);
            
            // Set universal hours (with defaults if missing)
            const universalHours = qm.universal_hours || { start: '23:00', end: '07:00' };
            document.getElementById('universal_start').value = universalHours.start;
            document.getElementById('universal_end').value = universalHours.end;
            
            // Set per-feature settings
            features.forEach(feature => {
                const featureSettings = qm.per_feature[feature];
                if (featureSettings) {
                    const checkbox = document.getElementById(`enable_${feature}_quiet`);
                    checkbox.checked = featureSettings.enabled;
                    
                    document.getElementById(`${feature}_start`).value = featureSettings.start;
                    document.getElementById(`${feature}_end`).value = featureSettings.end;
                    
                    // Show/hide time settings
                    toggleFeatureTimeSettings(feature, featureSettings.enabled);
                }
            });
        }
    } catch (error) {
        console.error('Error loading quiet mode settings:', error);
        showError('Failed to load quiet mode settings');
    }
}

async function toggleGlobalQuietMode(enabled) {
    const statusEl = document.getElementById('status_quiet_mode');
    
    // Show loading state
    if (statusEl) {
        statusEl.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Updating...';
        statusEl.className = 'feature-status loading';
    }
    
    try {
        const response = await fetch('/api/settings/quiet-mode/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        
        const result = await response.json();
        
        if (result.success) {
            updateGlobalStatus(enabled);
            showSuccess(`Quiet mode ${enabled ? 'enabled' : 'disabled'} successfully!`);
        } else {
            throw new Error(result.message || 'Failed to toggle quiet mode');
        }
    } catch (error) {
        console.error('Error toggling quiet mode:', error);
        showError('Failed to toggle quiet mode: ' + error.message);
        // Revert checkbox state
        document.getElementById('enable_quiet_mode').checked = !enabled;
        updateGlobalStatus(!enabled);
    }
}

function updateGlobalStatus(enabled) {
    const statusEl = document.getElementById('status_quiet_mode');
    if (statusEl) {
        if (enabled) {
            statusEl.innerHTML = '<i class="fas fa-check-circle"></i> Active';
            statusEl.className = 'feature-status enabled';
        } else {
            statusEl.innerHTML = '<i class="fas fa-times-circle"></i> Inactive';
            statusEl.className = 'feature-status disabled';
        }
    }
}

async function saveUniversalHours() {
    try {
        const start = document.getElementById('universal_start').value;
        const end = document.getElementById('universal_end').value;
        
        const response = await fetch('/api/settings/quiet-mode/universal', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ start, end })
        });
        
        const result = await response.json();
        
        if (result.success) {
            showSuccess('Universal quiet hours updated!');
        } else {
            throw new Error(result.message || 'Failed to update universal hours');
        }
    } catch (error) {
        console.error('Error saving universal hours:', error);
        showError('Failed to save universal hours: ' + error.message);
    }
}

async function toggleFeatureQuietHours(feature, enabled) {
    // Show/hide time settings immediately
    toggleFeatureTimeSettings(feature, enabled);
    
    try {
        const start = document.getElementById(`${feature}_start`).value;
        const end = document.getElementById(`${feature}_end`).value;
        
        const response = await fetch(`/api/settings/quiet-mode/feature/${feature}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled, start, end })
        });
        
        const result = await response.json();
        
        if (result.success) {
            const featureName = feature.charAt(0).toUpperCase() + feature.slice(1);
            showSuccess(`${featureName} custom quiet hours ${enabled ? 'enabled' : 'disabled'}!`);
        } else {
            throw new Error(result.message || 'Failed to update feature quiet hours');
        }
    } catch (error) {
        console.error('Error toggling feature quiet hours:', error);
        showError('Failed to update feature quiet hours: ' + error.message);
        // Revert checkbox state
        document.getElementById(`enable_${feature}_quiet`).checked = !enabled;
        toggleFeatureTimeSettings(feature, !enabled);
    }
}

async function saveFeatureHours(feature) {
    try {
        const enabled = document.getElementById(`enable_${feature}_quiet`).checked;
        const start = document.getElementById(`${feature}_start`).value;
        const end = document.getElementById(`${feature}_end`).value;
        
        console.log(`Saving ${feature} quiet hours:`, { enabled, start, end });
        
        const response = await fetch(`/api/settings/quiet-mode/feature/${feature}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled, start, end })
        });
        
        const result = await response.json();
        console.log(`${feature} save result:`, result);
        
        if (result.success) {
            const featureName = feature.charAt(0).toUpperCase() + feature.slice(1);
            showSuccess(`${featureName} quiet hours updated!`);
        } else {
            throw new Error(result.message || 'Failed to save feature hours');
        }
    } catch (error) {
        console.error('Error saving feature hours:', error);
        showError('Failed to save feature hours: ' + error.message);
    }
}

function toggleFeatureTimeSettings(feature, enabled) {
    const timeSettings = document.getElementById(`${feature}_time_settings`);
    if (timeSettings) {
        timeSettings.style.display = enabled ? 'block' : 'none';
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

