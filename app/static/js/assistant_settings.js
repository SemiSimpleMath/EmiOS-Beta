// Assistant Settings

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function() {
    loadAssistantSettings();

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

    document.querySelectorAll('input[name="communication_style"]').forEach(radio => {
        radio.addEventListener('change', updateChatGuidelines);
    });

    document.getElementById('assistantSettingsForm').addEventListener('submit', saveAssistantSettings);
});

// ── Assistant settings load/save ──────────────────────────────────────────────

async function loadAssistantSettings() {
    try {
        const res = await fetch('/api/assistant-core');
        if (!res.ok) throw new Error('Failed to load assistant core');
        const result = await res.json();
        if (!result.success) throw new Error(result.error || 'Unknown error');

        const identity = result.data.assistant_identity || {};
        const relationship = result.data.relationship || {};
        const guidelines = result.data.chat_guidelines || {};

        document.getElementById('assistant_name').value = identity.name || 'Emi';
        document.getElementById('assistant_personality').value = identity.personality || '';
        document.getElementById('assistant_backstory').value = identity.backstory || '';
        document.getElementById('assistant_core_directives').value = (identity.core_directives || []).join('\n');
        document.getElementById('assistant_voice_rules').value = (identity.voice_rules || []).join('\n');

        const relType = relationship.type || 'friend';
        document.getElementById('relationship_type').value = relType;
        document.getElementById('relationship_description').value = relationship.description || '';
        document.getElementById('assistant_role').value = relationship.role || '';
        if (relType) {
            document.getElementById('relationship_description_group').style.display = 'block';
        }

        const style = guidelines.style || 'direct';
        const styleRadio = document.querySelector(`input[name="communication_style"][value="${style}"]`);
        if (styleRadio) styleRadio.checked = true;
        document.getElementById('chat_guidelines').value = guidelines.guidelines || '';

        // Conversation starter frequency (defaults to 'regular' if not set yet)
        const cs = result.data.conversation_starter || {};
        const freq = cs.frequency || 'regular';
        const freqSel = document.getElementById('conversation_starter_frequency');
        if (freqSel) freqSel.value = freq;

    } catch (error) {
        console.error('Error loading assistant settings:', error);
        showError('Failed to load settings');
    }
}

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
    if (!guidelines.value.trim()) {
        guidelines.value = defaults[style] || '';
    }
}

async function saveAssistantSettings(e) {
    e.preventDefault();

    const coreDirectives = document.getElementById('assistant_core_directives').value.trim();
    const voiceRules = document.getElementById('assistant_voice_rules').value.trim();

    const coreData = {
        assistant_identity: {
            name: document.getElementById('assistant_name').value.trim(),
            personality: document.getElementById('assistant_personality').value.trim() || null,
            backstory: document.getElementById('assistant_backstory').value.trim() || null,
            core_directives: coreDirectives ? coreDirectives.split('\n').map(s => s.trim()).filter(Boolean) : [],
            voice_rules: voiceRules ? voiceRules.split('\n').map(s => s.trim()).filter(Boolean) : [],
        },
        relationship: {
            type: document.getElementById('relationship_type').value,
            description: document.getElementById('relationship_description').value.trim() || '',
            role: document.getElementById('assistant_role').value.trim()
        },
        chat_guidelines: {
            style: document.querySelector('input[name="communication_style"]:checked').value,
            guidelines: document.getElementById('chat_guidelines').value.trim()
        },
        conversation_starter: {
            frequency: (document.getElementById('conversation_starter_frequency')?.value || 'regular')
        }
    };

    document.getElementById('loading').style.display = 'block';

    try {
        const [coreRes] = await Promise.all([
            fetch('/api/assistant-core', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(coreData)
            }),
        ]);

        if (coreRes.ok) {
            showSuccess('Assistant settings saved successfully!');
        } else {
            throw new Error('Failed to save assistant core');
        }
    } catch (error) {
        console.error('Save error:', error);
        showError('Failed to save: ' + error.message);
    } finally {
        document.getElementById('loading').style.display = 'none';
    }
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function showError(message) {
    const errorDiv = document.getElementById('error-message');
    errorDiv.textContent = message;
    errorDiv.style.display = 'block';
    setTimeout(() => { errorDiv.style.display = 'none'; }, 5000);
}

function showSuccess(message) {
    const successDiv = document.getElementById('success-message');
    successDiv.textContent = message;
    successDiv.style.display = 'block';
    setTimeout(() => { successDiv.style.display = 'none'; }, 3000);
}
