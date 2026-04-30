"""
Debug Status Route - Shows user's physical status and location for debugging.
"""
from flask import Blueprint, render_template, jsonify
from datetime import datetime, timezone
from app.assistant.utils.time_utils import get_local_timezone

debug_status_bp = Blueprint('debug_status', __name__)


def _convert_timestamps_to_local(obj):
    """Recursively convert all ISO timestamp strings to local time with readable format."""
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            # Preserve explicit UTC fields (don't localize them)
            if isinstance(key, str) and key.endswith("_utc"):
                result[key] = value
            elif isinstance(value, str) and _looks_like_timestamp(value):
                result[key] = _format_local(value)
            else:
                result[key] = _convert_timestamps_to_local(value)
        return result
    elif isinstance(obj, list):
        return [_convert_timestamps_to_local(item) for item in obj]
    else:
        return obj


def _looks_like_timestamp(s: str) -> bool:
    """Check if string looks like an ISO timestamp."""
    if not s or len(s) < 10:
        return False
    # Look for ISO format patterns
    return ('T' in s and ('-' in s[:10] or ':' in s)) or s.endswith('Z') or '+00:00' in s


def _format_local(utc_str: str) -> str:
    """Convert UTC ISO string to readable local time."""
    try:
        # Parse the UTC timestamp
        if utc_str.endswith('Z'):
            utc_str = utc_str[:-1] + '+00:00'
        dt = datetime.fromisoformat(utc_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        
        # Convert to local
        local_tz = get_local_timezone()
        local_dt = dt.astimezone(local_tz)
        
        # Format as readable string with timezone abbreviation
        return local_dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except Exception:
        return utc_str  # Return original if parsing fails


def _get_sleep_segments_log(limit=50):
    """Get recent sleep segments from database."""
    try:
        from app.models.base import get_session
        from app.models.sleep_segments import SleepSegment
        
        session = get_session()
        try:
            segments = session.query(SleepSegment)\
                .order_by(SleepSegment.start_time.desc())\
                .limit(limit)\
                .all()
            
            result = []
            for seg in segments:
                result.append({
                    "id": seg.id,
                    "start": _format_local(seg.start_time.isoformat()),
                    "end": _format_local(seg.end_time.isoformat()) if seg.end_time else None,
                    "duration_minutes": seg.duration_minutes,
                    "duration_hours": round(seg.duration_minutes / 60, 1) if seg.duration_minutes else None,
                    "source": seg.source
                })
            return result
        finally:
            session.close()
    except Exception as e:
        return [{"error": str(e)}]


def _get_active_segments_log(limit=50):
    """Get recent active segments from database (source of truth for AFK computation)."""
    try:
        from app.assistant.afk_manager.afk_db import get_recent_active_segments

        segments = get_recent_active_segments(hours=72)[:limit]
        result = []
        for seg in segments:
            result.append({
                "id": seg.get("id"),
                "start_time": _format_local(seg["start_time"]) if seg.get("start_time") else None,
                "end_time": _format_local(seg["end_time"]) if seg.get("end_time") else None,
                "duration_minutes": seg.get("duration_minutes"),
                "created_at": _format_local(seg["created_at"]) if seg.get("created_at") else None,
            })
        return result
    except Exception as e:
        return [{"error": str(e)}]


def _get_wake_segments_log(limit=20):
    """Get recent wake segments from database."""
    try:
        from app.models.base import get_session
        from app.models.wake_segments import WakeSegment
        
        session = get_session()
        try:
            segments = session.query(WakeSegment)\
                .order_by(WakeSegment.start_time.desc())\
                .limit(limit)\
                .all()
            
            result = []
            for seg in segments:
                end_str = _format_local(seg.end_time.isoformat()) if seg.end_time else None
                result.append({
                    "id": seg.id,
                    "start_time": _format_local(seg.start_time.isoformat()),
                    "end_time": end_str,
                    "duration_minutes": seg.duration_minutes,
                    "source": seg.source,
                    "notes": seg.notes
                })
            return result
        finally:
            session.close()
    except Exception as e:
        return [{"error": str(e)}]



@debug_status_bp.route('/debug/status')
def status_page():
    """Render the status debug page."""
    return render_template('debug_status.html')


@debug_status_bp.route('/debug/status/data')
def status_data():
    """Return current status and location data as raw JSON."""
    import json
    import os
    from pathlib import Path
    
    try:
        # Get raw JSON from resource files
        from app.assistant.utils.path_utils import get_resources_dir

        resources_root = get_resources_dir()
        dayflow_dir = resources_root / "dayflow_pipeline_outputs"
        daily_insights_dir = resources_root / "daily_insights_pipeline_outputs"
        status_dir = resources_root / "status"
        context_user_dir = resources_root / "context" / "user"
        context_global_dir = resources_root / "context" / "global"

        tracked_config_dir = (
            Path(__file__).resolve().parents[1]
            / "assistant"
            / "pipelines"
            / "dayflow_"
            / "step_configs"
        )
        
        def load_resource(base_dir: Path, filename: str):
            """Helper to load a resource file from a specific subdir."""
            filepath = base_dir / filename
            try:
                return json.loads(filepath.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"_not_generated_yet": True, "message": f"File not generated yet. Trigger a refresh to generate it."}
            except Exception as e:
                return {"error": str(e)}

        def load_config(filename):
            """Helper to load a config file."""
            filepath = tracked_config_dir / filename
            try:
                return json.loads(filepath.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {"_not_found": True, "message": "Config file not found."}
            except Exception as e:
                return {"error": str(e)}
        
        # Load all resource files
        location_raw = load_resource(resources_root, "resource_current_location.json")
        tracked_activities_raw = load_resource(dayflow_dir, "resource_tracked_activities_output.json")
        tracked_activities_config_raw = load_config("config_tracked_activities.json")
        
        # Load NEW pipeline stage output files (resource_<stage_id>_output.json)
        sleep_output_raw = load_resource(dayflow_dir, "resource_sleep_output.json")
        afk_statistics_output_raw = load_resource(dayflow_dir, "resource_afk_statistics_output.json")
        # Note: activity_tracker stage writes to resource_tracked_activities_output.json (loaded above as tracked_activities_raw)
        daily_context_output_raw = load_resource(dayflow_dir, "resource_daily_context_generator_output.json")
        health_inference_output_raw = load_resource(dayflow_dir, "resource_health_inference_output.json")
        dayflow_orchestrator_output_raw = load_resource(dayflow_dir, "resource_dayflow_orchestrator_output.json")
        
        # Weather resource (from maintenance manager weather tool)
        weather_raw = load_resource(context_global_dir, "resource_weather.json")
        
        # Pipeline manager state
        pipeline_state_raw = load_resource(status_dir, "resource_wellness_pipeline_status.json")
        
        # Get database logs
        sleep_log = _get_sleep_segments_log(limit=50)
        active_log = _get_active_segments_log(limit=50)
        wake_log = _get_wake_segments_log(limit=20)
        
        local_tz = get_local_timezone()
        now_local = datetime.now(local_tz)
        
        return jsonify({
            "timestamp": now_local.strftime("%Y-%m-%d %I:%M:%S %p %Z"),
            "timezone": str(local_tz),
            
            # Raw (exact file contents, unmodified)
            "location_raw": location_raw,
            "tracked_activities_raw": tracked_activities_raw,
            "tracked_activities_config_raw": tracked_activities_config_raw,
            
            # Pipeline stage outputs (resource_<stage_id>_output.json)
            "pipeline_outputs_raw": {
                "sleep": sleep_output_raw,
                "afk_statistics": afk_statistics_output_raw,
                "activity_tracker": tracked_activities_raw,  # activity_tracker stage outputs to resource_tracked_activities_output.json
                "daily_context_generator": daily_context_output_raw,
                "health_inference": health_inference_output_raw,
                "dayflow_orchestrator": dayflow_orchestrator_output_raw,
            },
            
            # External data resources (populated by tools/managers)
            "external_resources_raw": {
                "weather": weather_raw,
            },
            
            # Pipeline manager state
            "pipeline_state_raw": pipeline_state_raw,
            
            # Database logs
            "sleep_log": sleep_log,
            "active_log": active_log,
            "wake_log": wake_log,
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@debug_status_bp.route('/debug/status/force_daily_reset', methods=['POST'])
def force_daily_reset():
    """Force a daily reset — no longer supported (legacy pipeline removed)."""
    return jsonify({"error": "force_daily_reset is no longer supported — dayflow_orchestrator_stage has been removed."}), 410

