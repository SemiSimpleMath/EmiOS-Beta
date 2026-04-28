# Knowledge Graph Visualizer

An interactive visualization system for the knowledge graph with real-time updates via WebSocket.

## Features

### Backend API
- RESTful API endpoints for graph data
- Node and edge search functionality
- Graph statistics and analytics
- Real-time WebSocket updates
- Node type and edge type filtering

### Frontend Visualization
- Interactive 2D force-directed graph
- Node and edge filtering by type
- Search and filter functionality
- Node details sidebar
- Real-time graph updates
- Responsive design with Tailwind CSS

## Project Structure

```
app/graph_visualizer/
├── __init__.py                 # Package initialization
├── api.py                      # Flask API endpoints
├── websocket.py                # WebSocket handlers
├── frontend/                   # React application
│   ├── package.json           # Node.js dependencies
│   ├── public/                # Static files
│   ├── src/                   # React source code
│   │   ├── App.tsx           # Main application component
│   │   ├── App.css           # Application styles
│   │   ├── index.tsx         # Application entry point
│   │   └── index.css         # Global styles
│   ├── tailwind.config.js    # Tailwind CSS configuration
│   ├── postcss.config.js     # PostCSS configuration
│   ├── tsconfig.json         # TypeScript configuration
│   └── README.md             # Frontend documentation
└── README.md                 # This file
```

## Setup Instructions

### Backend Setup

1. **Register the API Blueprint**

Add the graph API blueprint to your Flask app in `app/__init__.py`:

```python
from app.graph_visualizer.api import graph_api

# Register the blueprint
app.register_blueprint(graph_api)
```

2. **Setup WebSocket Support**

Add WebSocket support to your Flask app:

```python
from flask_socketio import SocketIO
from app.graph_visualizer.websocket import GraphWebSocket

# Initialize SocketIO
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize graph WebSocket handler
graph_websocket = GraphWebSocket(socketio)
```

3. **Update Knowledge Graph Utils**

Modify your knowledge graph utilities to broadcast updates:

```python
# In your knowledge graph utils, after adding/updating nodes/edges:
if hasattr(app, 'graph_websocket'):
    app.graph_websocket.broadcast_node_added(node_data)
    app.graph_websocket.broadcast_edge_added(edge_data)
```

### Frontend Setup

1. **Install Dependencies**

```bash
cd app/graph_visualizer/frontend
npm install
```

2. **Start Development Server**

```bash
npm start
```

The frontend will be available at http://localhost:3000

3. **Build for Production**

```bash
npm run build
```

## API Endpoints

### Graph Data
- `GET /api/graph` - Get all nodes and edges
- `GET /api/graph/search` - Search nodes and edges
- `GET /api/graph/node/<id>` - Get node details
- `GET /api/graph/stats` - Get graph statistics

### Node and Edge Types
- `GET /api/graph/node-types` - Get all node types
- `GET /api/graph/edge-types` - Get all edge types

## WebSocket Events

### Client to Server
- `connect` - Client connects
- `disconnect` - Client disconnects
- `join_graph` - Join graph updates room
- `leave_graph` - Leave graph updates room
- `request_graph_data` - Request current graph data

### Server to Client
- `connected` - Connection confirmed
- `joined_graph` - Joined graph room
- `left_graph` - Left graph room
- `graph_data` - Current graph data
- `node_added` - New node added
- `node_updated` - Node updated
- `node_deleted` - Node deleted
- `edge_added` - New edge added
- `edge_updated` - Edge updated
- `edge_deleted` - Edge deleted
- `graph_stats_updated` - Graph statistics updated

## Usage

### Basic Visualization

1. Start the Flask backend with WebSocket support
2. Start the React frontend
3. Navigate to http://localhost:3000
4. The graph will load automatically

### Interactive Features

- **Zoom and Pan**: Use mouse wheel and drag to navigate
- **Node Selection**: Click on nodes to view details
- **Search**: Use the search bar to find specific nodes
- **Filtering**: Use dropdowns to filter by node/edge type
- **Real-time Updates**: Graph updates automatically when data changes

### Node Details

When you click on a node, the sidebar shows:
- Basic node information
- Node properties
- Incoming and outgoing edges
- Confidence and strength scores

## Customization

### Node Colors

Modify the color scheme in `frontend/src/App.tsx`:

```typescript
const nodeColors: { [key: string]: string } = {
  'person': '#4299e1',
  'organization': '#48bb78',
  'place': '#ed8936',
  // Add more node types...
};
```

### Node Sizing

Adjust node size calculation in `frontend/src/App.tsx`:

```typescript
const getNodeSize = (node: any) => {
  return Math.max(3, Math.min(8, (node.confidence || 0.5) * 10));
};
```

### API Customization

Add new endpoints in `api.py`:

```python
@graph_api.route('/api/graph/custom', methods=['GET'])
def custom_endpoint():
    # Your custom logic here
    return jsonify({'data': 'custom'})
```

## Troubleshooting

### Common Issues

1. **CORS Errors**: Ensure CORS is properly configured for WebSocket
2. **Missing Dependencies**: Run `npm install` in the frontend directory
3. **TypeScript Errors**: Check that all dependencies are properly installed
4. **Graph Not Loading**: Verify the API endpoints are accessible

### Debug Mode

Enable debug mode in the frontend by adding console logs:

```typescript
const fetchGraphData = useCallback(async () => {
  try {
    console.log('Fetching graph data...');
    const response = await axios.get('/api/graph');
    console.log('Graph data received:', response.data);
    setGraphData(response.data);
  } catch (err) {
    console.error('Error fetching graph data:', err);
  }
}, []);
```

## Performance Considerations

- Large graphs (>1000 nodes) may require performance optimizations
- Consider implementing pagination for very large datasets
- Use WebSocket compression for real-time updates
- Implement graph clustering for better visualization

## Security

- Validate all API inputs
- Implement proper authentication for sensitive data
- Use HTTPS in production
- Sanitize user inputs in search functionality 