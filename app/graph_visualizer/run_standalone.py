#!/usr/bin/env python3
"""
Standalone Graph Visualizer Runner

This script runs the graph visualizer independently of the main Emi system.
It provides a complete web interface for visualizing knowledge graph data.

Usage:
    python run_standalone.py

Requirements:
    - Python 3.7+
    - Dependencies listed in requirements.txt
"""

import os

# Force set the environment variables BEFORE any imports
# Use production database (emidb) for graph visualization
os.environ['USE_TEST_DB'] = 'false'
# os.environ['TEST_DB_NAME'] = 'test_emidb'  # Not needed when USE_TEST_DB is false

import sys
import subprocess
import webbrowser
import time
from pathlib import Path

def check_dependencies():
    """Check if required dependencies are installed"""
    try:
        import flask
        import flask_socketio
        import flask_cors
        import sqlalchemy
        print("✅ All dependencies are installed")
        return True
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        print("Please install dependencies with: pip install -r requirements.txt")
        return False

def install_dependencies():
    """Install required dependencies"""
    print("📦 Installing dependencies...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("✅ Dependencies installed successfully")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to install dependencies: {e}")
        return False


def main():
    """Main function to run the standalone graph visualizer"""
    # Debug: Print environment variables
    print("=== GRAPH VISUALIZER ENVIRONMENT DEBUG ===")
    print(f"USE_TEST_DB: {os.environ.get('USE_TEST_DB')}")
    print(f"DEV_DATABASE_URI_EMI: {os.environ.get('DEV_DATABASE_URI_EMI')}")
    print(f"TEST_DB_NAME: {os.environ.get('TEST_DB_NAME')}")
    print("==========================================")
    
    print("🚀 Starting Graph Visualizer Standalone")
    print("=" * 50)
    
    # Check if we're in the right directory
    current_dir = Path(__file__).parent
    if not (current_dir / "standalone_app.py").exists():
        print("❌ Please run this script from the graph_visualizer directory")
        return
    
    # Check dependencies
    if not check_dependencies():
        print("\n📦 Installing dependencies...")
        if not install_dependencies():
            return

    print("\n🌐 Starting the server...")
    print("📊 The graph visualizer will be available at: http://localhost:5000")
    print("📡 WebSocket endpoint: ws://localhost:5000/socket.io")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 50)
    
    # Open browser after a short delay
    def open_browser():
        time.sleep(2)
        try:
            webbrowser.open('http://localhost:5000')
        except Exception as e:
            print(f"Could not open browser automatically: {e}")
    
    import threading
    browser_thread = threading.Thread(target=open_browser)
    browser_thread.daemon = True
    browser_thread.start()
    
    # Run the standalone app
    try:
        from standalone_app import create_standalone_app
        app, socketio = create_standalone_app()
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Error starting server: {e}")

if __name__ == "__main__":
    main() 