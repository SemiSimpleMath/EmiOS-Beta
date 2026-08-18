// Setup Wizard
let currentStep = 1;
let peopleCount = 0;
const totalSteps = 8;
let setupCompleted = false;

// Initialize when page loads
document.addEventListener('DOMContentLoaded', async function() {
    console.log('Setup wizard loaded');
    
    // Add Person button
    document.getElementById('add-person-btn').addEventListener('click', addPerson);
    
    // Navigation buttons
    document.getElementById('nextBtn').addEventListener('click', nextStep);
    document.getElementById('prevBtn').addEventListener('click', prevStep);
    document.getElementById('submitBtn').addEventListener('click', submitSetup);
    
    // Relationship type change
    document.getElementById('relationship_type').addEventListener('change', function() {
        const descGroup = document.getElementById('relationship_description_group');
        const descField = document.getElementById('relationship_description');
        
        const defaults = {
            friend: "You've known each other for years, so you're casual and genuine with each other.",
            boss: "You're professional and efficient, focusing on productivity and results.",
            collaborator: "You work together as equals, bouncing ideas off each other.",
            custom: ""
        };
        
        if (this.value) {
            descGroup.style.display = 'block';
            descField.value = defaults[this.value] || '';
        } else {
            descGroup.style.display = 'none';
        }
    });
    
    // Communication style change
    document.querySelectorAll('input[name="communication_style"]').forEach(radio => {
        radio.addEventListener('change', updateChatGuidelines);
    });
    
    // Provider selection
    document.querySelectorAll('input[name="llm_provider"]').forEach(radio => {
        radio.addEventListener('change', handleProviderChange);
    });

    // Google OAuth button
    const googleOAuthBtn = document.getElementById('google-oauth-btn');
    if (googleOAuthBtn) {
        googleOAuthBtn.addEventListener('click', handleGoogleOAuth);
    }
    
    // KG init / skip buttons
    const kgInitBtn = document.getElementById('kg-init-btn');
    const kgSkipBtn = document.getElementById('kg-skip-btn');
    if (kgInitBtn) kgInitBtn.addEventListener('click', handleKgInit);
    if (kgSkipBtn) kgSkipBtn.addEventListener('click', handleKgSkip);

    // Check OAuth status on page load
    checkGoogleOAuthStatus();
    
    // Check for OAuth success message in URL
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('oauth_success') === 'true') {
        showGoogleOAuthSuccess();
    }
    
    // Bio fact management
    document.querySelectorAll('.bio-add-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            const row = this.closest('.bio-add-row');
            const input = row.querySelector('.bio-new-fact');
            addBioFact(this.closest('.bio-section'), input);
        });
    });
    document.querySelectorAll('.bio-new-fact').forEach(input => {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                addBioFact(this.closest('.bio-section'), this);
            }
        });
    });

    // Try to restore a saved draft before adding the default person
    const restored = await restoreDraft();
    if (!restored) {
        addPerson();
        updateChatGuidelines();
    }
});

// ── Provider selection ────────────────────────────────────────────────────────

function apiUrl(path) {
    return (window.SCRIPT_NAME || '') + path;
}

function handleProviderChange() {
    const provider = document.querySelector('input[name="llm_provider"]:checked').value;

    // Toggle field panels
    ['openai', 'gemini', 'anthropic', 'opencode'].forEach(p => {
        const fields = document.getElementById(`fields-${p}`);
        const card = document.getElementById(`provider-card-${p}`);
        if (fields) fields.style.display = p === provider ? 'block' : 'none';
        if (card) card.classList.toggle('active', p === provider);
    });
}

function getSelectedProvider() {
    const radio = document.querySelector('input[name="llm_provider"]:checked');
    return radio ? radio.value : 'openai';
}

function getProviderApiKey() {
    const provider = getSelectedProvider();
    const fieldMap = {
        openai: 'openai_api_key',
        gemini: 'gemini_api_key',
        anthropic: 'anthropic_api_key',
        opencode: 'opencode_api_key',
    };
    const el = document.getElementById(fieldMap[provider]);
    return el ? el.value.trim() : '';
}

function getProviderModel() {
    const provider = getSelectedProvider();
    const fieldMap = {
        openai: 'openai_model',
        gemini: 'gemini_model',
        anthropic: 'anthropic_model',
        opencode: 'opencode_model',
    };
    const el = document.getElementById(fieldMap[provider]);
    return el ? el.value : '';
}

// ── KG initialisation ────────────────────────────────────────────────────────

async function handleKgInit() {
    const btn = document.getElementById('kg-init-btn');
    const skipBtn = document.getElementById('kg-skip-btn');
    const statusDiv = document.getElementById('kg-init-status');
    const messageSpan = document.getElementById('kg-init-message');

    btn.disabled = true;
    btn.textContent = 'Initializing…';
    skipBtn.disabled = true;
    statusDiv.style.display = 'block';
    statusDiv.style.backgroundColor = '#e3f2fd';
    statusDiv.style.border = '1px solid #2196f3';
    messageSpan.textContent = 'Loading embedding model and seeding core nodes (this may take 30–60 s on first run)…';

    try {
        const response = await fetch(apiUrl('/api/setup/seed-kg'), { method: 'POST' });
        const result = await response.json();

        if (result.success) {
            statusDiv.style.backgroundColor = '#e8f5e9';
            statusDiv.style.border = '1px solid #4caf50';
            messageSpan.innerHTML = '✓ Knowledge Graph initialized! Your personal node and important people have been added.';
            document.getElementById('kg-init-section').querySelector('.kg-init-actions').style.display = 'none';
        } else if (result.missing_deps) {
            statusDiv.style.backgroundColor = '#fff8e1';
            statusDiv.style.border = '1px solid #ffc107';
            messageSpan.innerHTML = `${result.error}<br><small>You can initialize the KG later from the KG Visualizer once the package is installed.</small>`;
            skipBtn.disabled = false;
            skipBtn.textContent = 'Continue to Chat';
        } else {
            throw new Error(result.error || 'KG initialization failed');
        }

        // Show a continue button after short delay
        setTimeout(() => {
            window.location.href = apiUrl('/chat_bot');
        }, result.success ? 3000 : 0);

    } catch (error) {
        console.error('KG init error:', error);
        statusDiv.style.backgroundColor = '#ffebee';
        statusDiv.style.border = '1px solid #f44336';
        messageSpan.textContent = 'Error: ' + error.message;
        btn.disabled = false;
        btn.textContent = 'Retry';
        skipBtn.disabled = false;
    }
}

function handleKgSkip() {
    window.location.href = apiUrl('/chat_bot');
}

// ── Existing helpers (unchanged) ─────────────────────────────────────────────

function updateChatGuidelines() {
    const style = document.querySelector('input[name="communication_style"]:checked').value;
    const guidelines = document.getElementById('chat_guidelines');
    
    const defaults = {
        direct: `Guidelines for chat:
- Don't try to drive conversation forward with questions unless it's natural
- Don't keep offering help unprompted
- No closing sentences like "I am here to help" or "Let me know if you need anything"
- Eliminate phrases like "just let me know"

Example:
User: Just working today.
Wrong: Nice, hope work's not too crazy today. If you need anything, just let me know!
Correct: Nice, hope work's not too crazy today.`,
        
        warm: `Guidelines for chat:
- Be empathetic and check in on wellbeing
- Offer help proactively when appropriate
- Use warm, supportive language
- Ask follow-up questions to show you care`,
        
        professional: `Guidelines for chat:
- Maintain professional boundaries
- Focus on efficiency and task completion
- Use structured, organized responses
- Confirm understanding and next steps clearly`,
        
        custom: ''
    };
    
    // Populate with default for the selected style
    // User can always edit regardless of which style is chosen
    guidelines.value = defaults[style] || '';
}

function addBioFact(sectionEl, input) {
    const text = (typeof input === 'string' ? input : input.value || '').trim();
    if (!text) return;
    const list = sectionEl.querySelector('.bio-facts-list');
    const item = document.createElement('div');
    item.className = 'bio-fact-item';
    item.innerHTML = `<span class="bio-fact-text">${escapeHtml(text)}</span><button type="button" class="bio-fact-remove" title="Remove">&times;</button>`;
    item.querySelector('.bio-fact-remove').addEventListener('click', function() { item.remove(); });
    list.appendChild(item);
    if (input && typeof input !== 'string' && input.focus) {
        input.value = '';
        input.focus();
    }
}

function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function collectBioData() {
    const bio = {};
    document.querySelectorAll('.bio-section').forEach(section => {
        const key = section.dataset.section;
        const facts = [];
        section.querySelectorAll('.bio-fact-text').forEach(el => {
            const t = el.textContent.trim();
            if (t) facts.push(t);
        });
        bio[key] = facts;
    });
    return bio;
}

function showStep(step) {
    // Hide all steps
    document.querySelectorAll('.setup-step').forEach(s => s.classList.remove('active'));
    
    // Show current step
    const currentStepEl = document.querySelector(`.setup-step[data-step="${step}"]`);
    if (currentStepEl) {
        currentStepEl.classList.add('active');
    }
    
    // Update progress bar
    document.querySelectorAll('.progress-step').forEach((s, index) => {
        if (index < step) {
            s.classList.add('active');
        } else {
            s.classList.remove('active');
        }
    });
    
    // Update navigation buttons
    const prevBtn = document.getElementById('prevBtn');
    const nextBtn = document.getElementById('nextBtn');
    const submitBtn = document.getElementById('submitBtn');
    
    prevBtn.style.display = step === 1 ? 'none' : 'inline-block';
    
    if (step === totalSteps) {
        nextBtn.style.display = 'none';
        submitBtn.style.display = 'none';
        
        // Update completion message
        const firstName = document.getElementById('first_name').value || document.getElementById('preferred_name').value;
        const assistantName = document.getElementById('assistant_name').value;
        document.getElementById('completion-name').textContent = firstName;
        document.getElementById('completion-assistant').textContent = assistantName;
    } else {
        nextBtn.style.display = 'inline-block';
        submitBtn.style.display = 'none';
    }
}

function nextStep() {
    console.log('Next button clicked, current step:', currentStep);
    
    if (validateStep(currentStep)) {
        if (currentStep < totalSteps - 1) {
            currentStep++;
            showStep(currentStep);
            saveDraft();
        } else if (currentStep === totalSteps - 1) {
            // Step 7 → Step 8: submit setup, then show Done step
            submitSetup();
        }
    }
}

function prevStep() {
    if (currentStep > 1) {
        currentStep--;
        showStep(currentStep);
        saveDraft();
    }
}

function validateStep(step) {
    const currentStepEl = document.querySelector(`.setup-step[data-step="${step}"]`);
    const inputs = currentStepEl.querySelectorAll('input[required], select[required]');
    
    for (let input of inputs) {
        if (!input.value.trim()) {
            showError(`Please fill in all required fields`);
            input.focus();
            return false;
        }
    }

    // Step 7: validate the active provider's API key
    if (step === 7) {
        const provider = getSelectedProvider();
        const apiKey = getProviderApiKey();
        if (!apiKey) {
            const labels = { openai: 'OpenAI', gemini: 'Google Gemini', anthropic: 'Anthropic', opencode: 'OpenCode Go' };
            showError(`Please enter your ${labels[provider]} API key`);
            return false;
        }
        if (provider === 'openai' && !apiKey.startsWith('sk-')) {
            showError('OpenAI API keys start with "sk-"');
            return false;
        }
        if (provider === 'anthropic' && !apiKey.startsWith('sk-ant-')) {
            showError('Anthropic API keys start with "sk-ant-"');
            return false;
        }
        const timezone = document.getElementById('timezone').value;
        if (!timezone) {
            showError('Please select your timezone');
            return false;
        }
    }
    
    // Step 2: Validate people
    if (step === 2) {
        const peopleEntries = document.querySelectorAll('.person-entry');
        if (peopleEntries.length > 0) {
            for (let entry of peopleEntries) {
                const name = entry.querySelector('.person-name').value.trim();
                const relationship = entry.querySelector('.person-relationship').value.trim();
                
                if (!name || !relationship) {
                    showError('Please fill in name and relationship for all people');
                    return false;
                }
            }
        }
    }
    
    return true;
}

function addPerson() {
    console.log('Adding person');
    peopleCount++;
    
    const template = document.getElementById('person-template');
    const clone = template.content.cloneNode(true);
    
    const personEntry = clone.querySelector('.person-entry');
    personEntry.dataset.personIndex = peopleCount;
    clone.querySelector('.person-number').textContent = peopleCount;
    
    const removeBtn = clone.querySelector('.remove-person-btn');
    removeBtn.addEventListener('click', function() {
        const entry = this.closest('.person-entry');
        removePerson(entry);
    });
    
    document.getElementById('people-list').appendChild(clone);
}

function removePerson(personEntry) {
    const peopleList = document.getElementById('people-list');
    const entries = peopleList.querySelectorAll('.person-entry');
    
    if (entries.length > 1) {
        personEntry.remove();
        renumberPeople();
    } else {
        showError('You must have at least one person entry');
    }
}

function renumberPeople() {
    const entries = document.querySelectorAll('.person-entry');
    entries.forEach((entry, index) => {
        entry.querySelector('.person-number').textContent = index + 1;
        entry.dataset.personIndex = index + 1;
    });
    peopleCount = entries.length;
}

function collectFormData() {
    // Collect people
    const people = [];
    document.querySelectorAll('.person-entry').forEach(entry => {
        const name = entry.querySelector('.person-name').value.trim();
        const relationship = entry.querySelector('.person-relationship').value.trim();
        const birthdate = entry.querySelector('.person-birthdate').value;
        
        if (name && relationship) {
            people.push({ name, relationship, birthdate: birthdate || null });
        }
    });
    
    // Build full name
    const firstName = document.getElementById('first_name').value.trim();
    const middleName = document.getElementById('middle_name').value.trim();
    const lastName = document.getElementById('last_name').value.trim();
    const fullName = [firstName, middleName, lastName].filter(n => n).join(' ');
    
    return {
        // Step 1
        first_name: firstName,
        middle_name: middleName || null,
        last_name: lastName,
        preferred_name: document.getElementById('preferred_name').value.trim() || null,
        full_name: fullName,
        birthdate: document.getElementById('birthdate').value || null,
        pronouns: document.getElementById('pronouns').value,
        
        // Step 2
        important_people: people,
        
        // Step 3
        job: document.getElementById('job').value.trim() || null,
        home_city: document.getElementById('home_city').value.trim() || null,
        additional_context: document.getElementById('additional_context').value.trim() || null,
        
        // Step 4
        assistant_name: document.getElementById('assistant_name').value.trim(),
        relationship_type: document.getElementById('relationship_type').value,
        relationship_description: document.getElementById('relationship_description').value.trim() || null,
        assistant_role: document.getElementById('assistant_role').value.trim(),
        assistant_personality: document.getElementById('assistant_personality').value.trim() || null,
        assistant_backstory: document.getElementById('assistant_backstory').value.trim() || null,
        
        // Step 5
        communication_style: document.querySelector('input[name="communication_style"]:checked').value,
        chat_guidelines: document.getElementById('chat_guidelines').value.trim(),
        
        // Step 4: Bio
        user_bio: collectBioData(),

        // Step 7
        llm_provider: getSelectedProvider(),
        llm_model: getProviderModel(),
        openai_api_key: (document.getElementById('openai_api_key') || {}).value?.trim() || null,
        gemini_api_key: (document.getElementById('gemini_api_key') || {}).value?.trim() || null,
        anthropic_api_key: (document.getElementById('anthropic_api_key') || {}).value?.trim() || null,
        opencode_api_key: (document.getElementById('opencode_api_key') || {}).value?.trim() || null,
        timezone: document.getElementById('timezone').value
    };
}

// ── Draft persistence ─────────────────────────────────────────────────────────

function saveDraft() {
    const data = collectFormData();
    data._step = currentStep;
    fetch(apiUrl('/api/setup/save-draft'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
    }).catch(err => console.warn('Draft save failed:', err));
}

async function restoreDraft() {
    try {
        const resp = await fetch(apiUrl('/api/setup/load-draft'));
        const result = await resp.json();
        if (!result.success || !result.draft) return false;
        const d = result.draft;

        // Step 1 — basic identity fields
        const simpleFields = {
            first_name: d.first_name,
            middle_name: d.middle_name,
            last_name: d.last_name,
            preferred_name: d.preferred_name,
            birthdate: d.birthdate,
            pronouns: d.pronouns,
            job: d.job,
            home_city: d.home_city,
            additional_context: d.additional_context,
            assistant_name: d.assistant_name,
            relationship_type: d.relationship_type,
            relationship_description: d.relationship_description,
            assistant_role: d.assistant_role,
            assistant_personality: d.assistant_personality,
            assistant_backstory: d.assistant_backstory,
            chat_guidelines: d.chat_guidelines,
            timezone: d.timezone,
            openai_api_key: d.openai_api_key,
            gemini_api_key: d.gemini_api_key,
            anthropic_api_key: d.anthropic_api_key,
        };
        for (const [id, val] of Object.entries(simpleFields)) {
            const el = document.getElementById(id);
            if (el && val != null) el.value = val;
        }

        // Step 2 — important people
        const people = d.important_people || [];
        const list = document.getElementById('people-list');
        list.innerHTML = '';
        peopleCount = 0;
        if (people.length === 0) {
            addPerson();
        } else {
            for (const p of people) {
                addPerson();
                const entries = list.querySelectorAll('.person-entry');
                const entry = entries[entries.length - 1];
                entry.querySelector('.person-name').value = p.name || '';
                entry.querySelector('.person-relationship').value = p.relationship || '';
                const bd = entry.querySelector('.person-birthdate');
                if (bd && p.birthdate) bd.value = p.birthdate;
            }
        }

        // Step 4 — bio facts
        if (d.user_bio) {
            for (const [section, facts] of Object.entries(d.user_bio)) {
                const sectionEl = document.querySelector(`.bio-section[data-section="${section}"]`);
                if (!sectionEl) continue;
                for (const fact of facts) {
                    addBioFact(sectionEl, fact);
                }
            }
        }

        // Communication style radio
        if (d.communication_style) {
            const radio = document.querySelector(`input[name="communication_style"][value="${d.communication_style}"]`);
            if (radio) radio.checked = true;
        }
        if (d.chat_guidelines) {
            document.getElementById('chat_guidelines').value = d.chat_guidelines;
        } else {
            updateChatGuidelines();
        }

        // Provider radio + fields
        if (d.llm_provider) {
            const radio = document.querySelector(`input[name="llm_provider"][value="${d.llm_provider}"]`);
            if (radio) { radio.checked = true; handleProviderChange(); }
        }
        // Model selects (set after provider change toggled visibility)
        if (d.llm_model) {
            const modelFields = { openai: 'openai_model', gemini: 'gemini_model', anthropic: 'anthropic_model', opencode: 'opencode_model' };
            const mf = document.getElementById(modelFields[d.llm_provider]);
            if (mf) mf.value = d.llm_model;
        }

        // Relationship description visibility
        if (d.relationship_type) {
            const descGroup = document.getElementById('relationship_description_group');
            if (descGroup) descGroup.style.display = 'block';
        }

        // Jump to saved step
        if (d._step && d._step > 1 && d._step <= totalSteps) {
            currentStep = d._step;
            showStep(currentStep);
        }

        console.log('Draft restored, step', currentStep);
        return true;
    } catch (err) {
        console.warn('Draft restore failed:', err);
        return false;
    }
}

async function submitSetup() {
    const formData = collectFormData();
    
    document.getElementById('loading').style.display = 'block';
    const nextBtn = document.getElementById('nextBtn');
    if (nextBtn) nextBtn.disabled = true;
    
    try {
        const response = await fetch(apiUrl('/api/setup/complete'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        });
        
        const result = await response.json();
        
        if (response.ok && result.success) {
            setupCompleted = true;
            document.getElementById('loading').style.display = 'none';
            // Advance to the Done step to show KG init option
            currentStep = totalSteps;
            showStep(currentStep);
        } else {
            throw new Error(result.error || 'Setup failed');
        }
    } catch (error) {
        console.error('Setup error:', error);
        showError('Setup failed: ' + error.message);
        document.getElementById('loading').style.display = 'none';
        if (nextBtn) nextBtn.disabled = false;
    }
}

function showError(message) {
    const errorDiv = document.getElementById('error-message');
    errorDiv.textContent = message;
    errorDiv.style.display = 'block';
    
    setTimeout(() => {
        errorDiv.style.display = 'none';
    }, 5000);
    
    errorDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Google OAuth Functions
async function checkGoogleOAuthStatus() {
    try {
        const response = await fetch(apiUrl('/api/oauth/google/status'));
        const data = await response.json();
        
        if (data.success && data.configured) {
            showGoogleOAuthSuccess();
        }
    } catch (error) {
        console.error('Error checking OAuth status:', error);
    }
}

async function handleGoogleOAuth() {
    const btn = document.getElementById('google-oauth-btn');
    const statusDiv = document.getElementById('google-oauth-status');
    const messageSpan = document.getElementById('google-oauth-message');
    
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Starting OAuth...';
    
    try {
        const response = await fetch(apiUrl('/api/oauth/google/start?redirect_to=setup'));
        const data = await response.json();
        
        if (data.success && data.authorization_url) {
            // Open OAuth flow in new window
            const width = 600;
            const height = 700;
            const left = (screen.width / 2) - (width / 2);
            const top = (screen.height / 2) - (height / 2);
            
            window.open(
                data.authorization_url,
                'Google OAuth',
                `width=${width},height=${height},left=${left},top=${top}`
            );
            
            statusDiv.style.display = 'block';
            statusDiv.style.backgroundColor = '#e3f2fd';
            statusDiv.style.border = '1px solid #2196f3';
            messageSpan.innerHTML = '<i class="fas fa-info-circle"></i> Please complete authentication in the popup window...';
        } else {
            throw new Error(data.error || 'Failed to start OAuth');
        }
    } catch (error) {
        console.error('OAuth error:', error);
        statusDiv.style.display = 'block';
        statusDiv.style.backgroundColor = '#ffebee';
        statusDiv.style.border = '1px solid #f44336';
        messageSpan.innerHTML = `<i class="fas fa-exclamation-circle"></i> Error: ${error.message}`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="fab fa-google"></i> Authenticate with Google';
    }
}

function showGoogleOAuthSuccess() {
    const statusDiv = document.getElementById('google-oauth-status');
    const messageSpan = document.getElementById('google-oauth-message');
    const btn = document.getElementById('google-oauth-btn');
    
    if (statusDiv && messageSpan) {
        statusDiv.style.display = 'block';
        statusDiv.style.backgroundColor = '#e8f5e9';
        statusDiv.style.border = '1px solid #4caf50';
        messageSpan.innerHTML = '<i class="fas fa-check-circle"></i> ✓ Google services connected successfully!';
        
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-check"></i> Connected';
            btn.style.backgroundColor = '#4caf50';
        }
    }
}

