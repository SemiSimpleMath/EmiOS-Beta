# app/routes/setup.py
"""
First-run setup wizard
"""
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from pathlib import Path
from app.assistant.utils.path_utils import get_resources_dir, setup_complete, get_env_file
import json
import uuid
from datetime import datetime

from app.models.base import get_session
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)

setup_bp = Blueprint('setup', __name__)

def is_setup_complete():
    """Check if initial setup has been completed"""
    return setup_complete()

def get_setup_status():
    """Get detailed setup status"""
    resources_dir = get_resources_dir()
    return {
        "resource_user_data": (resources_dir / "user" / "resource_user_data.json").exists(),
        "resource_assistant_data": (resources_dir / "assistant" / "resource_assistant_data.json").exists(),
        "resource_assistant_personality": (resources_dir / "assistant" / "resource_assistant_personality_data.json").exists(),
        "resource_relationship_config": (resources_dir / "assistant" / "resource_relationship_config.json").exists(),
        "resource_chat_guidelines": (resources_dir / "assistant" / "resource_chat_guidelines_data.json").exists(),
    }

@setup_bp.route('/setup', methods=['GET'])
def setup_wizard():
    """Render the setup wizard"""
    if is_setup_complete():
        # Setup already done, redirect to chat
        return redirect(url_for('chat_bot.chat_bot'))
    
    return render_template('setup_wizard.html')

@setup_bp.route('/api/setup/status', methods=['GET'])
def setup_status():
    """Get setup status"""
    return jsonify({
        'complete': is_setup_complete(),
        'details': get_setup_status()
    })

def _draft_path() -> Path:
    return get_resources_dir() / "user" / ".setup_draft.json"


@setup_bp.route('/api/setup/save-draft', methods=['POST'])
def save_draft():
    """Persist the wizard form state so progress survives restarts."""
    try:
        data = request.json or {}
        path = _draft_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return jsonify({'success': True})
    except Exception as e:
        logger.debug("Failed to save setup draft: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@setup_bp.route('/api/setup/load-draft', methods=['GET'])
def load_draft():
    """Return any previously saved wizard draft."""
    path = _draft_path()
    if not path.exists():
        return jsonify({'success': True, 'draft': None})
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return jsonify({'success': True, 'draft': data})
    except Exception as e:
        logger.debug("Failed to load setup draft: %s", e, exc_info=True)
        return jsonify({'success': True, 'draft': None})


def _generate_master_room_resources(data: dict, pronoun_subj: str, pronoun_obj: str) -> None:
    """Create the master_room resource JSON files from setup wizard answers."""
    room_dir = Path(__file__).resolve().parent.parent / "assistant" / "rooms" / "master_room"
    room_dir.mkdir(parents=True, exist_ok=True)

    user_name = data.get("preferred_name") or data["first_name"]
    assistant_name = data["assistant_name"]
    relationship_type = data.get("relationship_type", "friend")
    relationship_desc = data.get("relationship_description") or ""
    assistant_role = data.get("assistant_role") or "A helpful AI companion"
    personality = data.get("assistant_personality") or "Friendly and conversational"

    identity = {
        "content": (
            f"# Your identity\n"
            f"- You are {assistant_name}, an AI assistant for {user_name}.\n"
            f"- Your personality: {personality}\n"
            f"- Your role: {assistant_role}\n"
            f"- Be warm, natural, and concise by default.\n"
            f"- Avoid stiff assistant language and generic filler.\n"
            f"- For technical topics, be precise and efficient.\n"
            f"- You must always be honest and direct. Do not mislead or sugar-coat.\n"
            f"- Speak in first person (\"I\") as {assistant_name}.\n"
            f"- Refer to the user naturally as \"you\" unless a name is clearer."
        )
    }

    room_context = {
        "content": (
            f"# This room\n"
            f"- You are {assistant_name}. {user_name} is your {relationship_type} — you're talking to {pronoun_obj}.\n"
            + (f"- {relationship_desc}\n" if relationship_desc else "")
            + f"- In this room you are chatting directly with your primary user, {user_name}.\n"
            f"- This is the main owner room for direct UI chat.\n"
            f"- Treat this room as owner-scoped and personal. No topic is off bounds."
        )
    }

    conversation = {
        "content": (
            "# Conversation mechanics\n"
            "- Default to short, high-signal replies.\n"
            "- Ask follow-up questions only when they materially improve correctness.\n"
            "- Keep momentum and avoid repetitive confirmations.\n"
            "- Match the user's tone while staying grounded and clear.\n"
            "- Don't keep offering help unless the user asks.\n"
            "- Avoid closing lines like \"I am here to help\" or \"Let me know if you need anything\".\n"
            "- Cut filler like \"just let me know\"."
        )
    }

    safety = {
        "content": (
            "## Operational guardrails\n"
            f"{assistant_name} is a pure orchestrator. You do not execute tools directly. "
            "Your role is to classify user intent, gather necessary parameters, and "
            "delegate to specialized downstream agents once requirements are met.\n\n"
            "## Interaction complexity ladder\n"
            "| Level | Category | Action |\n"
            "| :--- | :--- | :--- |\n"
            "| C1 | Self-Contained | Delegate immediately unless disambiguation is required. |\n"
            "| C2 | Bounded Lookup | Delegate and report results; ask only for missing essentials. |\n"
            "| C3 | Parameterized | Ask 2-6 key constraints, then delegate. |\n"
            "| C4 | Transactional | Gather all details, present summary, wait for authorization. |\n"
            "| C5 | Open Project | Switch to Planning Mode. Clarify goals and constraints. |\n\n"
            "## Rules\n"
            "- Explicit consent required before: Delete, Send, Purchase, Cancel, or Reschedule.\n"
            "- No internal config changes, no account sign-ups, no direct file edits.\n"
            "- You may suggest reminders or todos, but never create them unless asked."
        )
    }

    # Update policy.json participant_identity with user's name
    policy_path = room_dir / "policy.json"
    if policy_path.exists():
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["participant_identity"] = {
                "display_name": user_name,
                "aliases": [user_name, data["first_name"]]
            }
            policy_path.write_text(json.dumps(policy, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("Failed to update policy.json: %s", exc, exc_info=True)

    for filename, content in [
        ("resource_identity.json", identity),
        ("resource_room_context.json", room_context),
        ("resource_conversation.json", conversation),
        ("resource_safety.json", safety),
    ]:
        (room_dir / filename).write_text(
            json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    logger.info("Generated master_room resource files for user=%s assistant=%s", user_name, assistant_name)


def _generate_system_room_resources(data: dict) -> None:
    """Create resource files for system rooms (dayflow_orchestrator, 1234 test room)."""
    rooms_dir = Path(__file__).resolve().parent.parent / "assistant" / "rooms"
    user_name = data.get("preferred_name") or data["first_name"]
    assistant_name = data["assistant_name"]

    _SYSTEM_ROOMS: dict[str, dict[str, dict]] = {
        "dayflow_orchestrator": {
            "resource_room_context.json": {"content": (
                "# This room\n"
                "- This is an internal orchestration room for dayflow planning and execution.\n"
                "- Operate as a system planner and dispatcher for dayflow intents.\n"
                "- Coordinate action-oriented follow-ups without duplicating handled items.\n"
                "- This room is not a direct end-user chat surface."
            )},
            "resource_conversation.json": {"content": (
                "# Conversation rules\n"
                "- Keep responses operational and concise.\n"
                "- Prefer explicit action-oriented phrasing for downstream delegates.\n"
                "- Avoid conversational fluff unless a user-facing response is required.\n"
                "- If no output is required, use no-op semantics instead of filler text."
            )},
            "resource_safety.json": {"content": (
                "# Safety and guardrails\n"
                "- Do not bypass approval requirements for sensitive external actions.\n"
                "- Do not fabricate tool outcomes, execution status, or user approvals.\n"
                "- Prefer deterministic delegation for structured execution tasks.\n"
                "- Fail loudly on invalid or missing required context."
            )},
            "resource_room_facts.json": {"content": (
                "# Room facts\n"
                f"- Primary owner: {user_name}. This room may consume owner-relevant context "
                "but should not leak cross-room data.\n"
                "- This room coordinates with master room context through explicit scope and policy.\n"
                "- It can delegate to specialist managers and tools for execution."
            )},
        },
        "1234": {
            "resource_identity.json": {"content": (
                f"# Your identity\n- You are {assistant_name} in a generic test room.\n"
                "- Keep communication practical, warm, and concise.\n"
                "- This room is used for room-manager integration checks."
            )},
            "resource_room_context.json": {"content": (
                "# This room\n- This is a test room for room-manager integration.\n"
                "- Preferred style is concise and actionable.\n"
                "- Prioritize clarity and low-latency responses."
            )},
            "resource_conversation.json": {"content": (
                "# Conversation mechanics\n- Prefer direct, short responses.\n"
                "- Ask one focused follow-up only when needed.\n"
                "- Keep responses actionable and easy to parse."
            )},
            "resource_safety.json": {"content": (
                "# Safety rules\n- Do not assume or invent private owner-only information.\n"
                "- If uncertain, state uncertainty and suggest a verification step.\n"
                "- Do not trigger external side effects unless policy allows it."
            )},
        },
    }

    for room_id, files in _SYSTEM_ROOMS.items():
        room_dir = rooms_dir / room_id
        room_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in files.items():
            path = room_dir / filename
            if not path.exists():
                path.write_text(json.dumps(content, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Generated system room resources for %s", room_id)


@setup_bp.route('/api/setup/complete', methods=['POST'])
def complete_setup():
    """Complete the setup process"""
    try:
        data = request.json
        
        # Validate required fields
        if not data.get('first_name') or not data.get('last_name'):
            return jsonify({'success': False, 'error': 'First and last name are required'}), 400
        if not data.get('pronouns'):
            return jsonify({'success': False, 'error': 'Pronouns are required'}), 400
        if not data.get('assistant_name'):
            return jsonify({'success': False, 'error': 'Assistant name is required'}), 400
        if not data.get('relationship_type'):
            return jsonify({'success': False, 'error': 'Relationship type is required'}), 400
        if not data.get('timezone'):
            return jsonify({'success': False, 'error': 'Timezone is required'}), 400

        # The provider registry is the single source of truth for WHICH providers
        # exist. Every list below derives from it, so adding a provider to
        # llm_classes_dict cannot leave this route behind.
        from app.configs.llm_classes_dict import _PROVIDER_KEY_ENV

        llm_provider = (data.get('llm_provider') or 'openai').lower()
        if llm_provider not in _PROVIDER_KEY_ENV:
            return jsonify({'success': False, 'error': f'Invalid LLM provider: {llm_provider}'}), 400

        # The wizard's form field is f"{provider}_api_key" for every provider,
        # so the mapping is the convention itself rather than a fifth table.
        api_key_field = f'{llm_provider}_api_key'
        if not data.get(api_key_field):
            # Display names only — a provider missing here still works, it just
            # shows its bare name in the error.
            _PROVIDER_LABEL = {
                'openai': 'OpenAI', 'gemini': 'Google Gemini',
                'anthropic': 'Anthropic', 'opencode': 'OpenCode Go',
            }
            provider_label = _PROVIDER_LABEL.get(llm_provider, llm_provider.title())
            return jsonify({'success': False, 'error': f'{provider_label} API key is required'}), 400
        
        # Create resources directory if it doesn't exist
        resources_root = get_resources_dir()
        user_resources_dir = resources_root / "user"
        assistant_resources_dir = resources_root / "assistant"
        user_resources_dir.mkdir(parents=True, exist_ok=True)
        assistant_resources_dir.mkdir(parents=True, exist_ok=True)
        
        # 0. Save API key and settings to .env file
        project_root = Path(__file__).resolve().parents[2]
        env_file = get_env_file()
        
        # Read existing .env if it exists
        env_content = {}
        if env_file.exists():
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        env_content[key.strip()] = value.strip()
        
        # Write the chosen provider's API key and mark it as default. The env
        # var name comes straight from the registry — this used to be a local
        # copy of _PROVIDER_KEY_ENV, which is exactly the kind of duplicate that
        # drifts (note gemini's key is GOOGLE_API_KEY, not GEMINI_API_KEY).
        api_key_value = data[api_key_field]
        env_content[_PROVIDER_KEY_ENV[llm_provider]] = api_key_value
        env_content['DEFAULT_LLM_PROVIDER'] = llm_provider
        llm_model = data.get('llm_model') or ''
        if llm_model:
            env_content['DEFAULT_LLM_MODEL'] = llm_model
        env_content['TIMEZONE'] = data['timezone']

        # Write back to .env (SETUP_COMPLETE is written at the very end after
        # all other steps succeed — see below)
        _KEY_ORDER = [
            'DEFAULT_LLM_PROVIDER', 'DEFAULT_LLM_MODEL',
            *_PROVIDER_KEY_ENV.values(),
            'TIMEZONE',
        ]
        with open(env_file, 'w') as f:
            f.write("# AI Assistant Configuration\n")
            f.write("# Generated by setup wizard\n\n")
            f.write("# LLM Provider Settings\n")
            for key in _KEY_ORDER:
                if key in env_content:
                    f.write(f"{key}={env_content[key]}\n")
            f.write("\n")
            for key, value in env_content.items():
                if key not in _KEY_ORDER:
                    f.write(f"{key}={value}\n")
        
        # Inject into live process so keys are available immediately without restart
        import os as _os
        for key in _KEY_ORDER:
            if key in env_content:
                _os.environ[key] = env_content[key]
        
        # 1. Create user_data.json
        # Split pronouns into subjective/objective (e.g., "he/him" -> "he", "him")
        pronouns = data['pronouns'].split('/')
        pronoun_subjective = pronouns[0] if len(pronouns) > 0 else data['pronouns']
        pronoun_objective = pronouns[1] if len(pronouns) > 1 else pronouns[0]
        
        user_data = {
            'first_name': data['first_name'],
            'middle_name': data.get('middle_name'),
            'last_name': data['last_name'],
            'preferred_name': data.get('preferred_name'),
            'full_name': data['full_name'],
            'pronouns': {
                'subjective': pronoun_subjective,
                'objective': pronoun_objective
            }
        }
        
        # Add optional fields
        if data.get('birthdate'):
            user_data['birthdate'] = data['birthdate']
        
        if data.get('job'):
            user_data['job'] = data['job']

        if data.get('home_city'):
            user_data['home_city'] = data['home_city']

        # Add important people
        important_people = data.get('important_people', [])
        if important_people:
            user_data['important_people'] = important_people
        
        # Add additional context
        if data.get('additional_context'):
            user_data['additional_context'] = data['additional_context']
        
        with open(user_resources_dir / "resource_user_data.json", "w") as f:
            json.dump(user_data, f, indent=2)

        # Home city is stored in resource_user_data.json (permanent).
        # Weather and location systems read it directly from there.

        # 2. Create resource_assistant_data.json (assistant's name - core identity)
        assistant_data = {
            'name': data['assistant_name']
        }
        
        with open(assistant_resources_dir / "resource_assistant_data.json", "w") as f:
            json.dump(assistant_data, f, indent=2)
        
        # 3. Create resource_assistant_personality_data.json (personality & backstory)
        assistant_personality = {}
        
        if data.get('assistant_personality'):
            assistant_personality['personality'] = data['assistant_personality']
        
        if data.get('assistant_backstory'):
            assistant_personality['backstory'] = data['assistant_backstory']
        
        with open(assistant_resources_dir / "resource_assistant_personality_data.json", "w") as f:
            json.dump(assistant_personality, f, indent=2)
        
        # 4. Create resource_relationship_config.json
        relationship_config = {
            'type': data['relationship_type'],
            'description': data.get('relationship_description', ''),
            'role': data.get('assistant_role', '')
        }
        
        with open(assistant_resources_dir / "resource_relationship_config.json", "w") as f:
            json.dump(relationship_config, f, indent=2)
        
        # 5. Create resource_chat_guidelines.json
        chat_guidelines = {
            'style': data.get('communication_style', 'direct'),
            'guidelines': data.get('chat_guidelines', '')
        }
        
        with open(assistant_resources_dir / "resource_chat_guidelines_data.json", "w") as f:
            json.dump(chat_guidelines, f, indent=2)
        
        # 6. Create user_bio.json from setup bio step. Lives under resources/user/
        # alongside other user-identity files (resource_user_data.json, etc.).
        user_resources_dir = resources_root / "user"
        user_resources_dir.mkdir(parents=True, exist_ok=True)
        raw_bio = data.get("user_bio") or {}
        user_bio = {
            "metadata": {
                "owner": data.get("preferred_name") or data["first_name"],
                "last_updated": datetime.now().strftime("%Y-%m-%d"),
                "schema_version": "1.0",
            },
        }
        for section in [
            "style_persona", "background", "projects", "values",
            "health_logistics", "entertainment_hobbies", "preferences",
        ]:
            facts = raw_bio.get(section)
            if isinstance(facts, list):
                user_bio[section] = [str(f).strip() for f in facts if str(f).strip()]
            else:
                user_bio[section] = []

        with open(user_resources_dir / "user_bio.json", "w") as f:
            json.dump(user_bio, f, indent=2, ensure_ascii=False)

        # 7. Generate room resource files from setup data
        _generate_master_room_resources(data, pronoun_subjective, pronoun_objective)
        _generate_system_room_resources(data)
        
        # 8. Initial entity cards: deferred.
        # The v1 entity_cards seeding here was retired on 2026-05-10 with the
        # rest of v1. Cards are now built by app.assistant.pipelines.entity_cards_v2
        # after the user's first KG ingest run (chat → KG → kg_nodes for user
        # + mentioned people → build_card per Group A entity). The setup wizard
        # itself doesn't need to mint kg_nodes — those land naturally during
        # the first chat session.

        # Everything succeeded — NOW mark setup as complete.
        # This is intentionally last so a partial failure doesn't lock the user
        # out of the wizard.
        import os as _os2
        env_content['SETUP_COMPLETE'] = 'true'
        _os2.environ['SETUP_COMPLETE'] = 'true'

        # Re-write .env with the flag included
        with open(env_file, 'w') as f:
            f.write("# AI Assistant Configuration\n")
            f.write("# Generated by setup wizard\n\n")
            _final_keys = list(_KEY_ORDER) + ['SETUP_COMPLETE']
            for key in _final_keys:
                if key in env_content:
                    f.write(f"{key}={env_content[key]}\n")
            f.write("\n")
            for key, value in env_content.items():
                if key not in _final_keys:
                    f.write(f"{key}={value}\n")

        session['setup_complete'] = True

        # Write a simple flag file so setup_complete() works across restarts
        # without relying on .env or checking resource file contents.
        from app.assistant.utils.path_utils import get_repo_root, get_data_dir
        (get_data_dir() / ".setup_complete").write_text("true\n", encoding="utf-8")

        # Clean up draft file
        try:
            draft = _draft_path()
            if draft.exists():
                draft.unlink()
        except Exception:
            pass
        
        return jsonify({
            'success': True,
            'message': 'Setup completed successfully!',
            'redirect': url_for('chat_bot.chat_bot')
        })
        
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@setup_bp.route('/api/setup/seed-kg', methods=['POST'])
def seed_kg():
    """Seed the Knowledge Graph with core nodes (user + assistant)."""
    try:
        from app.database.table_initializer import _seed_kg_core_nodes
        _seed_kg_core_nodes()
        return jsonify({'success': True, 'message': 'Knowledge Graph initialized successfully.'})
    except Exception as e:
        logger.debug("KG seed failed: %s", e, exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@setup_bp.route('/api/setup/reset', methods=['POST'])
def reset_setup():
    """Reset setup (for testing/reconfiguration)"""
    try:
        import os as _os
        resources_dir = get_resources_dir()

        # Delete resource files (matching the paths complete_setup writes to)
        files_to_delete = [
            resources_dir / "user" / "resource_user_data.json",
            resources_dir / "assistant" / "resource_assistant_data.json",
            resources_dir / "assistant" / "resource_assistant_personality_data.json",
            resources_dir / "assistant" / "resource_relationship_config.json",
            resources_dir / "assistant" / "resource_chat_guidelines_data.json",
            resources_dir / "user" / "user_bio.json",
        ]

        deleted = []
        for file_path in files_to_delete:
            if file_path.exists():
                file_path.unlink()
                deleted.append(str(file_path.name))

        # Clear SETUP_COMPLETE from environment
        _os.environ.pop('SETUP_COMPLETE', None)

        # Remove .setup_complete flag file
        from app.assistant.utils.path_utils import get_repo_root
        setup_flag = get_repo_root() / ".setup_complete"
        if setup_flag.exists():
            setup_flag.unlink()

        # Remove SETUP_COMPLETE from .env file
        project_root = Path(__file__).resolve().parents[2]
        env_file = get_env_file()
        if env_file.exists():
            lines = env_file.read_text(encoding="utf-8").splitlines()
            lines = [ln for ln in lines if not ln.strip().startswith("SETUP_COMPLETE")]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # Clean up draft file
        draft = _draft_path()
        if draft.exists():
            draft.unlink()

        # Clear session
        session.pop('setup_complete', None)

        return jsonify({
            'success': True,
            'message': f'Setup reset successfully. Removed: {", ".join(deleted) or "no files found"}.'
        })

    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

