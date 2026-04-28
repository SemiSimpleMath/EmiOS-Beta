"""
Agent Progress Route - real-time agent progress feed.
"""
from flask import Blueprint, render_template

progress_bp = Blueprint("progress", __name__)


@progress_bp.route("/progress")
def progress_tab():
    return render_template("progress.html")

