// API Keys Settings

document.addEventListener('DOMContentLoaded', function() {
    // Load current API key status and values
    loadAPIKeysSettings();
    
    // Form submit
    document.getElementById('apiKeysForm').addEventListener('submit', saveAPIKeysSettings);
});

async function loadAPIKeysSettings() {
    try {
        // Load API keys status
        const statusRes = await fetch('/api/settings/api-keys/status');
        if (statusRes.ok) {
            const statusData = await statusRes.json();
            if (statusData.success) {
                updateKeyStatus('openai', statusData.api_keys.openai.configured);
                updateKeyStatus('google', statusData.api_keys.google.configured);
                updateKeyStatus('anthropic', statusData.api_keys.anthropic.configured);
                updateKeyStatus('elevenlabs', statusData.api_keys.elevenlabs.configured);
                updateKeyStatus('deepgram', statusData.api_keys.deepgram.configured);
            }
        }
        
        // Load timezone and email settings (from resources or env)
        const envRes = await fetch('/api/env-settings');
        if (envRes.ok) {
            const envData = await envRes.json();
            if (envData.success && envData.settings) {
                document.getElementById('timezone').value = envData.settings.timezone || '';
                document.getElementById('gmail_address').value = envData.settings.email_addr || '';
                // Note: We don't populate API keys or passwords for security
            }
        }
    } catch (error) {
        console.error('Error loading API keys settings:', error);
        showError('Failed to load settings');
    }
}

function updateKeyStatus(provider, isConfigured) {
    const statusEl = document.getElementById(`${provider}_status`);
    if (statusEl) {
        statusEl.textContent = isConfigured ? '✓ Configured' : '✗ Not set';
        statusEl.className = 'key-status ' + (isConfigured ? 'configured' : 'not-configured');
    }
}

async function saveAPIKeysSettings(e) {
    e.preventDefault();
    
    const formData = {
        openai_api_key: document.getElementById('openai_api_key').value.trim() || null,
        google_api_key: document.getElementById('google_api_key').value.trim() || null,
        anthropic_api_key: document.getElementById('anthropic_api_key').value.trim() || null,
        elevenlabs_api_key: document.getElementById('elevenlabs_api_key').value.trim() || null,
        deepgram_api_key: document.getElementById('deepgram_api_key').value.trim() || null,
        timezone: document.getElementById('timezone').value,
        gmail_address: document.getElementById('gmail_address').value.trim() || null,
        gmail_app_password: document.getElementById('gmail_app_password').value.trim() || null
    };
    
    // Validation
    if (!formData.timezone) {
        showError('Timezone is required');
        return;
    }
    
    document.getElementById('loading').style.display = 'block';
    
    try {
        const response = await fetch('/api/env-settings', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        });
        
        const result = await response.json();
        
        if (result.success) {
            showSuccess('API keys saved successfully! You may need to restart the application for changes to take effect.');
            // Clear password fields
            document.getElementById('openai_api_key').value = '';
            document.getElementById('google_api_key').value = '';
            document.getElementById('anthropic_api_key').value = '';
            document.getElementById('elevenlabs_api_key').value = '';
            document.getElementById('deepgram_api_key').value = '';
            document.getElementById('gmail_app_password').value = '';
            // Reload status
            setTimeout(() => loadAPIKeysSettings(), 1000);
        } else {
            throw new Error(result.error || 'Save failed');
        }
    } catch (error) {
        console.error('Save error:', error);
        showError('Failed to save: ' + error.message);
    } finally {
        document.getElementById('loading').style.display = 'none';
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
    }, 5000);
}


