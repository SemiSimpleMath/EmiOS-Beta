# Graph Visualizer - Standalone Mode

This guide shows you how to run the graph visualizer completely independently of the main Emi system.

## 🚀 Quick Start

### Option 1: Simple Runner Script
```bash
cd app/graph_visualizer
python run_standalone.py
```

### Option 2: Manual Setup
```bash
cd app/graph_visualizer
pip install -r requirements.txt
python standalone_app.py
```

## 📁 What's Included

The standalone version includes:

- **Complete Flask API** (`api.py`) - All graph data endpoints
- **WebSocket Support** (`websocket.py`) - Real-time updates
- **Built-in Frontend** (`templates/graph_visualizer.html`) - No React build needed
- **Demo Data** - Sample graph for testing
- **Dependency Management** (`requirements.txt`) - All needed packages

## 🔧 Features

### ✅ What Works Independently

- **Graph Visualization** - Interactive D3.js force-directed graph
- **API Endpoints** - All CRUD operations for nodes/edges
- **Search & Filter** - Find nodes by type, label, properties
- **Real-time Updates** - WebSocket broadcasts for live changes
- **Node Details** - Click nodes to see properties and connections
- **Demo Data** - Sample graph with people, organizations, places

### 🔗 What Connects to Emi (Optional)

- **Database** - Uses the same knowledge graph database
- **Models** - Same SQLAlchemy models for consistency
- **Pipeline Integration** - Can receive updates from Emi agents

## 🎯 Use Cases

### 1. **Development & Testing**
```bash
# Quick testing of graph visualization
python run_standalone.py
```

### 2. **Demo & Presentations**
```bash
# Show graph capabilities without full Emi setup
python standalone_app.py
```

### 3. **Data Exploration**
```bash
# Explore existing knowledge graph data
python run_standalone.py
```

### 4. **Custom Integration**
```python
# Import and use in your own applications
from app.graph_visualizer.api import graph_api
from app.graph_visualizer.websocket import GraphWebSocket
```

## 📊 API Endpoints

All endpoints work independently:

```
GET  /api/graph              - Get all nodes and edges
GET  /api/graph/search        - Search with filters
GET  /api/graph/node/<id>     - Get node details
GET  /api/graph/stats         - Get graph statistics
GET  /api/graph/node-types    - Get node types
GET  /api/graph/edge-types    - Get edge types
GET  /health                  - Health check
```

## 🌐 WebSocket Events

Real-time updates work independently:

**Client → Server:**
- `connect` - Connect to WebSocket
- `join_graph` - Join graph updates room
- `request_graph_data` - Get current data

**Server → Client:**
- `graph_data` - Current graph data
- `node_added` - New node added
- `edge_added` - New edge added
- `node_updated` - Node updated
- `edge_updated` - Edge updated

## 🎨 Frontend Features

The built-in frontend includes:

- **Interactive Graph** - Zoom, pan, click nodes
- **Color Coding** - Different colors for node types
- **Search Bar** - Find nodes by label
- **Type Filters** - Filter by node/edge type
- **Node Details** - Sidebar with properties
- **Real-time Updates** - Live graph changes

## 🔧 Configuration

### Database Connection

The standalone version uses the same database as Emi:

```python
# In standalone_app.py
from app.models.base import engine
from app.assistant.kg.db.knowledge_graph_db import Node, Edge
```

### Custom Database

To use a different database:

```python
# Modify app/models/base.py
SQLALCHEMY_DATABASE_URI = "sqlite:///your_graph.db"
```

### Port Configuration

Change the port in `standalone_app.py`:

```python
socketio.run(app, host='0.0.0.0', port=8080, debug=True)
```

## 📦 Dependencies

The standalone version requires:

```
Flask==2.3.3
Flask-SocketIO==5.3.6
Flask-CORS==4.0.0
python-socketio==5.9.0
python-engineio==4.7.1
SQLAlchemy==2.0.21
psycopg2-binary==2.9.7
```

## 🚀 Deployment

### Local Development
```bash
python run_standalone.py
# Open http://localhost:5000
```

### Production Deployment
```bash
# Using Gunicorn
pip install gunicorn
gunicorn -w 4 -k uvicorn.workers.UvicornWorker standalone_app:app

# Using Docker
docker build -t graph-visualizer .
docker run -p 5000:5000 graph-visualizer
```

### Docker Setup
```dockerfile
FROM python:3.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 5000

CMD ["python", "standalone_app.py"]
```

## 🔍 Troubleshooting

### Common Issues

1. **Port Already in Use**
```bash
# Change port in standalone_app.py
socketio.run(app, port=5001)
```

2. **Database Connection Error**
```bash
# Check database path
ls app/app.db
```

3. **Missing Dependencies**
```bash
pip install -r requirements.txt
```

4. **WebSocket Connection Issues**
```javascript
// Check browser console for WebSocket errors
// Ensure CORS is properly configured
```

### Debug Mode

Enable debug logging:

```python
# In standalone_app.py
socketio.run(app, debug=True, log_output=True)
```

## 🔄 Integration with Emi

### Option 1: Shared Database
- Both systems use the same database
- Changes in Emi appear in visualizer
- Real-time updates work

### Option 2: Separate Database
- Copy data to separate database
- Independent operation
- No interference with main system

### Option 3: API Integration
- Visualizer calls Emi's API
- Separate deployments
- Network communication

## 📈 Performance

### Optimization Tips

1. **Large Graphs** (>1000 nodes)
   - Implement pagination
   - Use graph clustering
   - Add node filtering

2. **Real-time Updates**
   - Batch updates
   - Throttle WebSocket events
   - Use compression

3. **Memory Usage**
   - Limit concurrent connections
   - Implement cleanup routines
   - Monitor memory usage

## 🔐 Security

### Production Considerations

1. **Authentication**
```python
# Add authentication to API endpoints
from flask_login import login_required

@graph_api.route('/api/graph')
@login_required
def get_graph_data():
    # Your code here
```

2. **CORS Configuration**
```python
# Restrict CORS origins
CORS(app, resources={r"/api/*": {"origins": ["https://yourdomain.com"]}})
```

3. **Rate Limiting**
```python
# Add rate limiting
from flask_limiter import Limiter
limiter = Limiter(app)

@graph_api.route('/api/graph')
@limiter.limit("100 per minute")
def get_graph_data():
    # Your code here
```

## 📚 Examples

### Custom Node Types
```python
# Add custom node colors
node_colors = {
    'custom_type': '#ff6b6b',
    'another_type': '#4ecdc4'
}
```

### Custom API Endpoints
```python
@graph_api.route('/api/graph/custom', methods=['GET'])
def custom_endpoint():
    return jsonify({'custom': 'data'})
```

### WebSocket Events
```python
# Broadcast custom events
graph_websocket.socketio.emit('custom_event', data, room='graph_updates')
```

## 🎯 Next Steps

1. **Run the standalone version** to test functionality
2. **Customize the frontend** for your needs
3. **Add authentication** for production use
4. **Deploy to your infrastructure**
5. **Integrate with your data sources**

The standalone graph visualizer gives you a complete, working system that you can use immediately without any dependency on the main Emi system! 