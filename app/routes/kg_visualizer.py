"""
KG Visualizer Route
Serves the React build for the Knowledge Graph Visualizer
"""

from flask import Blueprint, send_from_directory, abort
from pathlib import Path
import os

kg_visualizer_bp = Blueprint('kg_visualizer', __name__)

# Path to the React build directory
FRONTEND_BUILD_DIR = Path(__file__).resolve().parents[1] / 'graph_visualizer' / 'frontend' / 'build'

@kg_visualizer_bp.route('/kg-visualizer')
def kg_visualizer_index():
    """Serve the main KG Visualizer React app"""
    try:
        return send_from_directory(FRONTEND_BUILD_DIR, 'index.html')
    except FileNotFoundError:
        return """
        <h1>KG Visualizer Not Built</h1>
        <p>The React frontend has not been built yet.</p>
        <p>To build it, run:</p>
        <pre>
cd app/graph_visualizer/frontend
npm install
npm run build
        </pre>
        """, 404

@kg_visualizer_bp.route('/kg-visualizer/<path:filename>')
def kg_visualizer_static(filename):
    """Serve static assets from the React build"""
    try:
        return send_from_directory(FRONTEND_BUILD_DIR, filename)
    except FileNotFoundError:
        abort(404)

@kg_visualizer_bp.route('/kg-visualizer/static/<path:path>')
def kg_visualizer_static_assets(path):
    """Serve static JS/CSS assets from the React build"""
    static_dir = FRONTEND_BUILD_DIR / 'static'
    try:
        return send_from_directory(static_dir, path)
    except FileNotFoundError:
        abort(404)


