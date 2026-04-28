"""Dev routes for subsystem toggle controls and file serving."""
from flask import Blueprint, jsonify, request, render_template_string, send_file, abort

subsystem_route_bp = Blueprint("subsystem_controls", __name__)

# Friendly labels for each subsystem.
_SUBSYSTEM_LABELS = {
    "dayflow_scheduler": "Dayflow Scheduler",
    "routine_manager": "Routine Manager",
    "background_tasks": "Background Tasks",
    "signal_router": "Signal Router",
    "dj_manager": "DJ Manager",
    "context_engine": "Context Engine",
    "kg_chat_pipeline": "KG Chat Pipeline (nightly ingest)",
    "kg_proposal_promoter": "KG Proposal Promoter (nightly promote)",
}


@subsystem_route_bp.route("/dev/subsystems", methods=["GET"])
def subsystems_page():
    """Render the subsystem toggles dev page."""
    from app.assistant.utils.subsystem_flags import get_all_flags

    flags = get_all_flags()

    # Build ordered list with labels.
    items = []
    for key, label in _SUBSYSTEM_LABELS.items():
        items.append({"key": key, "label": label, "enabled": bool(flags.get(key, True))})
    # Include any flags in the file not in our label map.
    for key, value in flags.items():
        if key not in _SUBSYSTEM_LABELS:
            items.append({"key": key, "label": key.replace("_", " ").title(), "enabled": bool(value)})

    return render_template_string(_PAGE_TEMPLATE, items=items)


@subsystem_route_bp.route("/api/dev/subsystems", methods=["GET"])
def get_subsystem_flags():
    """API: return current subsystem flags."""
    from app.assistant.utils.subsystem_flags import get_all_flags
    return jsonify({"success": True, "flags": get_all_flags()})


@subsystem_route_bp.route("/api/dev/subsystems/<name>", methods=["POST"])
def toggle_subsystem(name: str):
    """API: toggle a single subsystem flag."""
    from app.assistant.utils.subsystem_flags import set_subsystem_flag

    body = request.get_json(silent=True) or {}
    enabled = body.get("enabled")
    if enabled is None:
        return jsonify({"success": False, "message": "Missing 'enabled' field"}), 400

    try:
        flags = set_subsystem_flag(name, bool(enabled))
        return jsonify({"success": True, "name": name, "enabled": bool(enabled), "flags": flags})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@subsystem_route_bp.route("/dev/file/<path:filepath>", methods=["GET"])
def serve_dev_file(filepath: str):
    """Serve a file from the repo root. Used for playwright screenshots in daily summary."""
    from pathlib import Path
    from app.assistant.utils.path_utils import get_repo_root
    repo_root = Path(get_repo_root())
    full_path = (repo_root / filepath).resolve()
    # Security: must be within repo root.
    if not str(full_path).startswith(str(repo_root.resolve())):
        abort(403)
    if not full_path.is_file():
        abort(404)
    return send_file(full_path)


_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Subsystem Controls</title>
  <link rel="stylesheet" href="/static/css/base.css">
  <link rel="stylesheet" href="/static/css/emi_tokens.css">
  <style>
    body {
      font-family: var(--font-family, 'Inter', sans-serif);
      background: var(--color-bg, #0f1117);
      color: var(--color-text, #e0e0e0);
      margin: 0;
      padding: 2rem;
    }
    .container {
      max-width: 600px;
      margin: 0 auto;
    }
    h1 {
      font-size: 1.5rem;
      margin-bottom: 0.5rem;
      color: var(--color-text-heading, #fff);
    }
    .subtitle {
      color: var(--color-text-muted, #888);
      font-size: 0.85rem;
      margin-bottom: 2rem;
    }
    .toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0.75rem 1rem;
      margin-bottom: 0.5rem;
      background: var(--color-surface, #1a1d27);
      border-radius: 8px;
      border: 1px solid var(--color-border, #2a2d37);
    }
    .toggle-label {
      font-size: 0.95rem;
      font-weight: 500;
    }
    .toggle-key {
      font-size: 0.75rem;
      color: var(--color-text-muted, #888);
      font-family: monospace;
    }
    /* Toggle switch */
    .switch {
      position: relative;
      width: 44px;
      height: 24px;
      flex-shrink: 0;
    }
    .switch input {
      opacity: 0;
      width: 0;
      height: 0;
    }
    .slider {
      position: absolute;
      cursor: pointer;
      inset: 0;
      background: #444;
      border-radius: 24px;
      transition: background 0.2s;
    }
    .slider::before {
      content: "";
      position: absolute;
      width: 18px;
      height: 18px;
      left: 3px;
      bottom: 3px;
      background: white;
      border-radius: 50%;
      transition: transform 0.2s;
    }
    input:checked + .slider {
      background: #4caf50;
    }
    input:checked + .slider::before {
      transform: translateX(20px);
    }
    .status-msg {
      text-align: center;
      margin-top: 1rem;
      font-size: 0.85rem;
      color: var(--color-text-muted, #888);
      min-height: 1.2em;
    }
    .warning {
      margin-top: 1.5rem;
      padding: 0.75rem 1rem;
      background: #2a2000;
      border: 1px solid #554400;
      border-radius: 8px;
      font-size: 0.8rem;
      color: #dda;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1>Subsystem Controls</h1>
    <p class="subtitle">Toggle EmiOS subsystems on/off. Changes take effect on next subsystem check.</p>

    {% for item in items %}
    <div class="toggle-row">
      <div>
        <div class="toggle-label">{{ item.label }}</div>
        <div class="toggle-key">{{ item.key }}</div>
      </div>
      <label class="switch">
        <input type="checkbox" data-subsystem="{{ item.key }}" {{ 'checked' if item.enabled else '' }}>
        <span class="slider"></span>
      </label>
    </div>
    {% endfor %}

    <div id="status-msg" class="status-msg"></div>

    <div class="warning">
      Some subsystems only check their flag at startup or on a timer. Toggling here updates the config file immediately, but the subsystem may not respond until its next check cycle.
    </div>
  </div>

  <script>
    document.querySelectorAll('input[data-subsystem]').forEach(function(toggle) {
      toggle.addEventListener('change', async function() {
        const name = this.dataset.subsystem;
        const enabled = this.checked;
        const statusEl = document.getElementById('status-msg');
        try {
          const resp = await fetch('/api/dev/subsystems/' + name, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: enabled}),
          });
          const data = await resp.json();
          if (data.success) {
            statusEl.textContent = name + ' → ' + (enabled ? 'enabled' : 'disabled');
            statusEl.style.color = enabled ? '#4caf50' : '#f44336';
          } else {
            statusEl.textContent = 'Error: ' + (data.message || 'unknown');
            statusEl.style.color = '#f44336';
            toggle.checked = !enabled;
          }
        } catch(e) {
          statusEl.textContent = 'Request failed';
          statusEl.style.color = '#f44336';
          toggle.checked = !enabled;
        }
        setTimeout(function() { statusEl.textContent = ''; }, 3000);
      });
    });
  </script>
</body>
</html>
"""
