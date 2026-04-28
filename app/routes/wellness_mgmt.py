"""
Wellness Management Route - View wellness tracking status.
"""
from flask import Blueprint, jsonify
import json
from pathlib import Path
from app.assistant.utils.path_utils import get_resources_dir

wellness_mgmt_bp = Blueprint('wellness_mgmt', __name__)


@wellness_mgmt_bp.route('/debug/wellness/status')
def wellness_status():
    """Get current wellness tracking status."""
    try:
        resources_root = get_resources_dir()
        dayflow_dir = resources_root / "dayflow_pipeline_outputs"
        status_dir = resources_root / "status"

        def load_resource(base_dir: Path, filename: str):
            filepath = base_dir / filename
            try:
                return json.loads(filepath.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"_not_generated_yet": True}
            except Exception as e:
                return {"error": str(e)}

        pipeline_state = load_resource(status_dir, "resource_wellness_pipeline_status.json")
        tracked_activities = load_resource(dayflow_dir, "resource_tracked_activities_output.json")
        sleep_output = load_resource(dayflow_dir, "resource_sleep_output.json")

        return jsonify({
            "pipeline_state": pipeline_state,
            "tracked_activities": tracked_activities,
            "sleep_output": sleep_output,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500
