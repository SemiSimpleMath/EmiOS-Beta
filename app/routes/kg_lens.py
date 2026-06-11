"""Static-file blueprint for KG Lens (`/kg-lens`).

Serves the Vite production build at `app/kg_lens/frontend/dist/`. The
/api/kg-lens/... endpoints are registered separately via
app/kg_lens/api.py's blueprint in app/create_app.py.
"""
from __future__ import annotations

from flask import Blueprint, abort, send_from_directory

from app.assistant.utils.path_utils import get_app_root

kg_lens_bp = Blueprint("kg_lens", __name__)

FRONTEND_DIST = get_app_root() / "kg_lens" / "frontend" / "dist"


@kg_lens_bp.route("/kg-lens")
@kg_lens_bp.route("/kg-lens/")
def kg_lens_index():
    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        return (
            "<h1>KG Lens not built</h1>"
            "<p>The Vite frontend has not been built yet.</p>"
            "<pre>cd app/kg_lens/frontend\nnpm install\nnpm run build</pre>",
            404,
        )
    return send_from_directory(FRONTEND_DIST, "index.html")


@kg_lens_bp.route("/kg-lens/<path:filename>")
def kg_lens_static(filename: str):
    target = FRONTEND_DIST / filename
    if not target.exists() or target.is_dir():
        # SPA route: any unknown path serves index.html so client routing
        # can take over.
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return send_from_directory(FRONTEND_DIST, "index.html")
        abort(404)
    return send_from_directory(FRONTEND_DIST, filename)
