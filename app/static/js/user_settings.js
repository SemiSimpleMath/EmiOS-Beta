// User Settings
let peopleCount = 0;

document.addEventListener('DOMContentLoaded', function() {
    // Load current settings
    loadUserSettings();
    
    // Add Person button
    document.getElementById('add-person-btn').addEventListener('click', addPerson);
    
    // Form submit
    document.getElementById('userSettingsForm').addEventListener('submit', saveUserSettings);
});

async function loadUserSettings() {
    try {
        const response = await fetch('/api/user-profile');
        const data = await response.json();
        
        if (data.success && data.profile) {
            const profile = data.profile;
            
            // Fill in user data
            document.getElementById('first_name').value = profile.first_name || '';
            document.getElementById('middle_name').value = profile.middle_name || '';
            document.getElementById('last_name').value = profile.last_name || '';
            document.getElementById('preferred_name').value = profile.preferred_name || '';
            document.getElementById('birthdate').value = profile.birthdate || '';
            
            // Set pronouns
            if (profile.pronouns) {
                const pronounStr = `${profile.pronouns.subjective}/${profile.pronouns.objective}`;
                document.getElementById('pronouns').value = pronounStr;
            }
            
            document.getElementById('job').value = profile.job || '';
            document.getElementById('home_city').value = profile.home_city || '';
            document.getElementById('additional_context').value = profile.additional_context || '';
            
            // Load important people
            if (profile.important_people && profile.important_people.length > 0) {
                profile.important_people.forEach(person => {
                    addPerson(person);
                });
            } else {
                // Add one empty person entry
                addPerson();
            }
        }
    } catch (error) {
        console.error('Error loading user settings:', error);
        showError('Failed to load settings');
    }
}

function addPerson(personData = null) {
    peopleCount++;
    
    const template = document.getElementById('person-template');
    const clone = template.content.cloneNode(true);
    
    const personEntry = clone.querySelector('.person-entry');
    personEntry.dataset.personIndex = peopleCount;
    clone.querySelector('.person-number').textContent = peopleCount;
    
    // Fill in data if provided
    if (personData) {
        clone.querySelector('.person-name').value = personData.name || '';
        clone.querySelector('.person-relationship').value = personData.relationship || '';
        clone.querySelector('.person-birthdate').value = personData.birthdate || '';
    }
    
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

async function saveUserSettings(e) {
    e.preventDefault();
    
    // Collect people data
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
    
    const formData = {
        first_name: firstName,
        middle_name: middleName || null,
        last_name: lastName,
        preferred_name: document.getElementById('preferred_name').value.trim() || null,
        full_name: fullName,
        birthdate: document.getElementById('birthdate').value || null,
        pronouns: document.getElementById('pronouns').value,
        job: document.getElementById('job').value.trim() || null,
        home_city: document.getElementById('home_city').value.trim() || null,
        additional_context: document.getElementById('additional_context').value.trim() || null,
        important_people: people
    };
    
    document.getElementById('loading').style.display = 'block';
    
    try {
        const response = await fetch('/api/user-profile', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(formData)
        });
        
        const result = await response.json();
        
        if (result.success) {
            showSuccess('Settings saved successfully!');
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
    }, 3000);
}


