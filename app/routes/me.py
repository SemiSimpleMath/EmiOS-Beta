"""Static-file blueprint for the lens (`/me`).

Serves the Vite production build at `app/me/frontend/dist/`. The /api/me/...
endpoints are registered separately via app/me/api.py's blueprint, which is
imported and registered in app/__init__.py.

If the frontend bundle isn't built yet, returns a small dev-time message
explaining how to build it.
"""
from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, send_from_directory

me_bp = Blueprint("me", __name__)

FRONTEND_DIST = Path(__file__).resolve().parents[1] / "me" / "frontend" / "dist"


@me_bp.route("/me")
@me_bp.route("/me/")
def me_index():
    index = FRONTEND_DIST / "index.html"
    if not index.exists():
        return (
            "<h1>Lens not built</h1>"
            "<p>The Vite frontend has not been built yet.</p>"
            "<pre>cd app/me/frontend\nnpm install\nnpm run build</pre>",
            404,
        )
    return send_from_directory(FRONTEND_DIST, "index.html")


@me_bp.route("/me/<path:filename>")
def me_static(filename: str):
    target = FRONTEND_DIST / filename
    if not target.exists() or target.is_dir():
        # SPA fallback: any unknown path serves index.html so client routing
        # can take over.
        index = FRONTEND_DIST / "index.html"
        if index.exists():
            return send_from_directory(FRONTEND_DIST, "index.html")
        abort(404)
    return send_from_directory(FRONTEND_DIST, filename)
