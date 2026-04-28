from flask import Flask, render_template, make_response
from flask_socketio import SocketIO
from flask_cors import CORS
import os
import sys

# Add the parent directory to the path to import app modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.graph_visualizer.api import graph_api
from app.graph_visualizer.websocket import GraphWebSocket

def create_standalone_app():
    """Create a standalone Flask app for the graph visualizer"""
    
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY')
    if not app.config['SECRET_KEY']:
        raise RuntimeError("FLASK_SECRET_KEY environment variable is not set. Set it in your .env file.")
    
    # Enable CORS for all routes
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
    # Register the graph API blueprint
    app.register_blueprint(graph_api)
    
    # Initialize SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
    
    # Initialize graph WebSocket handler
    graph_websocket = GraphWebSocket(socketio)
    
    # Store socketio instance on app for access in other modules
    app.socketio = socketio
    app.graph_websocket = graph_websocket
    
    @app.route('/')
    @app.route('/<version>')
    def index(version=None):
        """Serve the main visualization page"""
        print(f"🔍 Serving graph_visualizer.html template (version: {version})")
        print(f"📁 Template path: {app.template_folder}")
        print(f"📄 Template file: graph_visualizer.html")
        
        try:
            response = make_response(render_template('graph_visualizer.html'))
            response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '0'
            print(f"✅ Template rendered successfully")
            return response
        except Exception as e:
            print(f"❌ Error rendering template: {e}")
            return f"Error: {e}", 500
    
    @app.route('/health')
    def health():
        """Health check endpoint"""
        return {'status': 'healthy', 'service': 'graph-visualizer'}
    
    @app.route('/debug-template')
    def debug_template():
        """Debug endpoint to check template content"""
        try:
            with open('app/graph_visualizer/templates/graph_visualizer.html', 'r') as f:
                content = f.read()
            return {
                'template_exists': True,
                'template_size': len(content),
                'first_100_chars': content[:100],
                'has_test_banner': 'TEST BANNER' in content,
                'has_cache_bust': 'CACHE BUST' in content
            }
        except Exception as e:
            return {'error': str(e), 'template_exists': False}
    
    return app, socketio

if __name__ == '__main__':
    app, socketio = create_standalone_app()
    
    print("🚀 Starting Graph Visualizer Standalone Server...")
    print("📊 API Endpoints:")
    print("   - GET  /api/graph              - Get all nodes and edges")
    print("   - GET  /api/graph/search        - Search nodes and edges")
    print("   - GET  /api/graph/node/<id>     - Get node details")
    print("   - GET  /api/graph/stats         - Get graph statistics")
    print("   - GET  /api/graph/node-types    - Get node types")
    print("   - GET  /api/graph/edge-types    - Get edge types")
    print("   - GET  /health                  - Health check")
    print("")
    print("🌐 Frontend: http://localhost:5000")
    print("📡 WebSocket: ws://localhost:5000/socket.io")
    print("")
    print("Press Ctrl+C to stop the server")
    
    try:
        socketio.run(app, host='0.0.0.0', port=5000, debug=True, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Error starting server: {e}") 