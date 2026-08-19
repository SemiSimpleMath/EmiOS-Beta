"""
Insights API — serves daily assessments, weekly insights, and beliefs.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root, get_resources_dir

logger = get_logger(__name__)

insights_bp = Blueprint("insights", __name__)


def _day_context_dir() -> Path:
    return get_repo_root() / "day_context"


def _resolve_day_path(date_str: str) -> Path | None:
    """Resolve day_context path for a given YYYY-MM-DD date."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return _day_context_dir() / str(dt.year) / f"{dt.month:02d}" / date_str


@insights_bp.route("/insights")
def insights_page():
    return render_template("insights.html")


@insights_bp.route("/api/insights/daily")
def api_daily_insights():
    date_str = request.args.get("date", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    day_dir = _resolve_day_path(date_str)
    if day_dir is None or not day_dir.exists():
        return jsonify({"success": False, "message": f"No data for {date_str}"})

    # Try assessment summary first (richest), then daily insights
    summary_path = day_dir / "resource_daily_assessment_summary.json"
    insights_path = day_dir / "resource_daily_insights.json"

    # Load daily learnings (actionable_information) if available
    learnings = []
    if insights_path.exists():
        try:
            insights_data = json.loads(insights_path.read_text(encoding="utf-8"))
            learnings = insights_data.get("actionable_information", [])
        except Exception:
            pass

    if summary_path.exists():
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
            result = {
                "success": True,
                "date": date_str,
                "source": "daily_assessment_summary",
                "summary": data.get("summary", {}),
                "learnings": learnings,
            }
            return jsonify(result)
        except Exception as e:
            logger.error("Failed reading daily assessment summary for %s: %s", date_str, e)
            return jsonify({"success": False, "message": f"Error reading data: {e}"})

    if insights_path.exists():
        try:
            data = json.loads(insights_path.read_text(encoding="utf-8"))
            # Adapt insights format to match summary structure
            return jsonify({
                "success": True,
                "date": date_str,
                "source": "daily_insights",
                "summary": {
                    "title": data.get("title", f"Insights for {date_str}"),
                    "executive_summary": data.get("insights", []) if isinstance(data.get("insights"), list) else [],
                    "dominant_themes": data.get("themes", []),
                    "belief_candidates": data.get("belief_candidates", []),
                },
            })
        except Exception as e:
            logger.error("Failed reading daily insights for %s: %s", date_str, e)
            return jsonify({"success": False, "message": f"Error reading data: {e}"})

    return jsonify({"success": False, "message": f"No insights generated yet for {date_str}"})


@insights_bp.route("/api/insights/weekly")
def api_weekly_insights():
    date_str = request.args.get("date", "").strip()
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    try:
        ref_date = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Invalid date format"})

    # Find the weekly insights file that covers this date
    weekly_dir = get_resources_dir() / "weekly_insights_pipeline_outputs"

    # Check latest first
    latest_path = weekly_dir / "resource_weekly_insights_latest.json"
    if latest_path.exists():
        try:
            data = json.loads(latest_path.read_text(encoding="utf-8"))
            week_start = data.get("week_start", "")
            week_end = data.get("week_end", "")

            # Check if this weekly report covers the requested date
            if week_start and week_end:
                ws = datetime.strptime(week_start, "%Y-%m-%d")
                we = datetime.strptime(week_end, "%Y-%m-%d")
                if ws <= ref_date <= we:
                    return jsonify({
                        "success": True,
                        "week_start": week_start,
                        "week_end": week_end,
                        "insights": data.get("insights", {}),
                    })
        except Exception as e:
            logger.error("Failed reading weekly insights: %s", e)

    # Search archived weekly files in week_context/{year}/{iso_week}/
    week_context_dir = get_repo_root() / "week_context"
    if week_context_dir.exists():
        for f in sorted(week_context_dir.rglob("resource_weekly_insights.json"), reverse=True):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                week_start = data.get("week_start", "")
                week_end = data.get("week_end", "")
                if week_start and week_end:
                    ws = datetime.strptime(week_start, "%Y-%m-%d")
                    we = datetime.strptime(week_end, "%Y-%m-%d")
                    if ws <= ref_date <= we:
                        return jsonify({
                            "success": True,
                            "week_start": week_start,
                            "week_end": week_end,
                            "insights": data.get("insights", {}),
                        })
            except Exception:
                continue

    return jsonify({"success": False, "message": f"No weekly insights found covering {date_str}"})


@insights_bp.route("/api/insights/beliefs")
def api_beliefs():
    beliefs_path = get_resources_dir() / "kg_derived" / "resource_user_beliefs.json"

    if not beliefs_path.exists():
        return jsonify({"success": False, "message": "No beliefs file found"})

    try:
        data = json.loads(beliefs_path.read_text(encoding="utf-8"))
        beliefs = data.get("beliefs", [])
        if not isinstance(beliefs, list):
            beliefs = []
        return jsonify({
            "success": True,
            "count": len(beliefs),
            "beliefs": beliefs,
            "metadata": data.get("_metadata", {}),
        })
    except Exception as e:
        logger.error("Failed reading beliefs: %s", e)
        return jsonify({"success": False, "message": f"Error reading beliefs: {e}"})
