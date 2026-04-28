2#!/usr/bin/env python3
"""
Start the Graph Visualizer

This script starts the graph visualizer with proper setup and testing.
"""

import sys
import subprocess
from pathlib import Path

def setup_environment():
    """Setup the environment for the graph visualizer"""
    print("🔧 Setting up Graph Visualizer Environment")
    print("=" * 50)
    
    # Add the app directory to Python path
    app_dir = Path(__file__).parent.parent
    if str(app_dir) not in sys.path:
        sys.path.insert(0, str(app_dir))
    
    # Initialize the database
    try:
        from app.assistant.kg.db.knowledge_graph_db import initialize_knowledge_graph_db
        print("📊 Initializing knowledge graph database...")
        initialize_knowledge_graph_db()
        print("✅ Database initialized successfully")
    except Exception as e:
        print(f"⚠️ Database initialization warning: {e}")
    
    # Create demo data if needed
    try:
        from app.graph_visualizer.run_standalone import create_demo_data
        print("📊 Creating demo data...")
        create_demo_data()
    except Exception as e:
        print(f"⚠️ Demo data creation warning: {e}")

def start_backend():
    """Start the Flask backend server"""
    print("\n🚀 Starting Graph Visualizer Backend")
    print("=" * 50)
    
    # Check if we have the standalone app
    standalone_app = Path(__file__).parent / "standalone_app.py"
    if standalone_app.exists():
        print("📡 Starting standalone Flask server...")
        print("🌐 Backend will be available at: http://localhost:5000")
        print("📊 API endpoints:")
        print("   - GET /api/graph - Get all graph data")
        print("   - GET /api/graph/search - Search nodes/edges")
        print("   - GET /api/graph/stats - Get graph statistics")
        print("\nPress Ctrl+C to stop the server")
        print("=" * 50)
        
        # Start the server
        subprocess.run([sys.executable, str(standalone_app)])
    else:
        print("❌ Standalone app not found. Please ensure standalone_app.py exists.")

def start_frontend():
    """Start the React frontend"""
    print("\n🎨 Starting Graph Visualizer Frontend")
    print("=" * 50)
    
    frontend_dir = Path(__file__).parent / "frontend"
    if not frontend_dir.exists():
        print("❌ Frontend directory not found")
        return
    
    print("📦 Installing frontend dependencies...")
    try:
        subprocess.run(["npm", "install"], cwd=frontend_dir, check=True)
        print("✅ Dependencies installed")
    except subprocess.CalledProcessError:
        print("❌ Failed to install dependencies")
        return
    except FileNotFoundError:
        print("❌ npm not found. Please install Node.js and npm")
        return
    
    print("🚀 Starting React development server...")
    print("🌐 Frontend will be available at: http://localhost:3000")
    print("\nPress Ctrl+C to stop the server")
    print("=" * 50)
    
    try:
        subprocess.run(["npm", "start"], cwd=frontend_dir)
    except KeyboardInterrupt:
        print("\n👋 Frontend server stopped")

def main():
    """Main function"""
    print("🎯 Graph Visualizer Setup")
    print("=" * 50)
    
    # Setup environment
    setup_environment()
    
    # Ask user what to start
    print("\nWhat would you like to start?")
    print("1. Backend only (Flask API)")
    print("2. Frontend only (React)")
    print("3. Both (in separate terminals)")
    print("4. Test API endpoints")
    
    choice = input("\nEnter your choice (1-4): ").strip()
    
    if choice == "1":
        start_backend()
    elif choice == "2":
        start_frontend()
    elif choice == "3":
        print("\n🔄 Starting both backend and frontend...")
        print("You'll need to open two terminal windows.")
        print("\nTerminal 1 (Backend):")
        print(f"cd {Path(__file__).parent}")
        print("python start_visualizer.py")
        print("Choose option 1")
        print("\nTerminal 2 (Frontend):")
        print(f"cd {Path(__file__).parent}")
        print("python start_visualizer.py")
        print("Choose option 2")
        
        # Start backend in current terminal
        start_backend()
    elif choice == "4":
        print("\n🧪 Testing API endpoints...")
        try:
            from test_api import test_api_endpoints
            test_api_endpoints()
        except ImportError:
            print("❌ Test script not found")
    else:
        print("❌ Invalid choice")

if __name__ == "__main__":
    main() 