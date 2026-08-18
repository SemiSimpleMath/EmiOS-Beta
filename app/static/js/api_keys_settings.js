// API Keys Settings

// Set from request.script_root by the template. Empty when EmiOS is served at
// the domain root; "/emi" (or whatever EMI_PROXY_SUBPATH says) behind a
// prefix-stripping proxy. Every fetch below must go through it.
//
// This page does not load Emi.js, so it gets no auto-prefixing fetch wrapper
// and must prefix explicitly. Reading the same window.SCRIPT_NAME that Emi.js
// uses keeps one name for one value: if this page ever did pull in Emi.js,
// that wrapper would see an already-prefixed URL and leave it alone, whereas
// two separate globals would have silently double-prefixed it.
const BASE = window.SCRIPT_NAME || '';

// Providers with a password input + status badge on this page. The id prefix is
// both the form field (`<prefix>_api_key`), the status span (`<prefix>_status`)
// and the JSON field the backend expects (`<prefix>_api_key`), so one list
// drives load, save and clear — adding a provider means adding it here and in
// the template, nowhere else.
const KEY_PROVIDERS = [
    'openai', 'opencode', 'google', 'anthropic', 'elevenlabs', 'deepgram',
];

document.addEventListener('DOMContentLoaded', function() {
    // Load current API key status and values
    loadAPIKeysSettings();
    
    // Form submit
    document.getElementById('apiKeysForm').addEventListener('submit', saveAPIKeysSettings);
});

async function loadAPIKeysSettings() {
    try {
        // Load API keys status
        const statusRes = await fetch(`${BASE}/api/settings/api-keys/status`);
        if (statusRes.ok) {
            const statusData = await statusRes.json();
            if (statusData.success) {
                // Index defensively: a provider the backend doesn't report yet
                // used to throw on `.configured` and abort the whole load,
                // blanking the timezone/email fields too.
                KEY_PROVIDERS.forEach(p => {
                    const entry = (statusData.api_keys || {})[p];
                    updateKeyStatus(p, Boolean(entry && entry.configured));
                });
            }
        }

        // Load timezone and email settings (from resources or env)
        const envRes = await fetch(`${BASE}/api/env-settings`);
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
        timezone: document.getElementById('timezone').value,
        gmail_address: document.getElementById('gmail_address').value.trim() || null,
        gmail_app_password: document.getElementById('gmail_app_password').value.trim() || null
    };

    KEY_PROVIDERS.forEach(p => {
        const input = document.getElementById(`${p}_api_key`);
        formData[`${p}_api_key`] = input ? (input.value.trim() || null) : null;
    });

    // Validation
    if (!formData.timezone) {
        showError('Timezone is required');
        return;
    }
    
    document.getElementById('loading').style.display = 'block';
    
    try {
        const response = await fetch(`${BASE}/api/env-settings`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        });
        
        const result = await response.json();
        
        if (result.success) {
            showSuccess('API keys saved successfully! You may need to restart the application for changes to take effect.');
            // Clear password fields
            KEY_PROVIDERS.forEach(p => {
                const input = document.getElementById(`${p}_api_key`);
                if (input) input.value = '';
            });
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


