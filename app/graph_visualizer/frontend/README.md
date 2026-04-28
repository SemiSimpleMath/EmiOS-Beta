# Knowledge Graph Visualizer Frontend

A modern, interactive React-based visualization for the EmiAi knowledge graph system with enhanced features and improved user experience.

## ✨ New Features

### 🎨 Enhanced UI/UX
- **Modern Design**: Gradient backgrounds, improved typography, and smooth animations
- **Responsive Layout**: Works seamlessly on desktop, tablet, and mobile devices
- **Dark Mode Support**: Automatic dark mode detection and styling
- **Accessibility**: Keyboard navigation, screen reader support, and reduced motion preferences
- **Loading States**: Beautiful loading spinners and error handling

### 📊 Graph Statistics Panel
- **Real-time Metrics**: Node count, edge count, node types, edge types
- **Graph Analytics**: Average node degree and graph density calculations
- **Toggle Visibility**: Show/hide statistics panel as needed
- **Visual Cards**: Color-coded metric cards with hover effects

### 🔍 Advanced Search & Filtering
- **Smart Search**: Search across node labels, aliases, and descriptions
- **Type Filtering**: Filter by node types and edge types
- **Real-time Filtering**: Instant results as you type
- **Clear Highlights**: Reset all filters and highlights with one click

### 🎯 Interactive Graph Features
- **Node Highlighting**: Click nodes to highlight connected elements
- **Edge Highlighting**: Click edges to highlight connected nodes
- **Visual Feedback**: Highlighted nodes and edges are larger and colored differently
- **Smooth Animations**: Hover effects and transitions throughout

### 🔄 Auto-refresh System
- **Configurable Intervals**: 10s, 30s, 1m, or 5m refresh options
- **Toggle Control**: Enable/disable auto-refresh as needed
- **Background Updates**: Seamless data updates without interrupting user interaction

### 📱 Enhanced Sidebar
- **Card-based Layout**: Organized information in visually appealing cards
- **Relationship Visualization**: Clear display of incoming and outgoing relationships
- **Attribute Management**: Toggle between formatted view and raw JSON
- **Timeline Information**: Creation and update timestamps
- **Responsive Design**: Adapts to different screen sizes

### 🎨 Visual Improvements
- **Extended Color Palette**: 80+ node type colors for better categorization
- **Dynamic Node Sizing**: Nodes sized based on importance and confidence scores
- **Enhanced Labels**: Better text rendering with background highlights
- **Info Overlay**: Real-time graph statistics overlay

## 🚀 Getting Started

### Prerequisites
- Node.js 16+ 
- npm or yarn

### Installation
```bash
cd app/graph_visualizer/frontend
npm install
```

### Development
```bash
npm start
```
The app will be available at http://localhost:3000

### Production Build
```bash
npm run build
```

## 🛠️ Technical Features

### Performance Optimizations
- **Memoized Filtering**: Efficient graph data filtering with React.useMemo
- **Lazy Loading**: Components load only when needed
- **Optimized Rendering**: Efficient canvas rendering for large graphs
- **Debounced Search**: Prevents excessive API calls during typing

### State Management
- **Local State**: React hooks for component state management
- **Persistent Filters**: Search and filter state maintained during navigation
- **Highlight Management**: Efficient tracking of highlighted elements

### API Integration
- **RESTful Endpoints**: Integration with Flask backend API
- **Error Handling**: Graceful error handling with retry mechanisms
- **Loading States**: Proper loading indicators for all async operations

## 🎯 Usage Guide

### Basic Navigation
1. **View Graph**: The main graph displays all nodes and relationships
2. **Search**: Use the search bar to find specific nodes or concepts
3. **Filter**: Use dropdown menus to filter by node or edge types
4. **Click Elements**: Click nodes or edges to view detailed information
5. **Clear Highlights**: Use the "Clear Highlights" button to reset the view

### Advanced Features
1. **Statistics Panel**: Toggle the stats panel to view graph metrics
2. **Auto-refresh**: Enable auto-refresh for real-time updates
3. **Sidebar Details**: Explore detailed information in the collapsible sidebar
4. **JSON View**: Toggle between formatted and raw JSON views for attributes

### Keyboard Shortcuts
- `Escape`: Close sidebar
- `Enter`: Trigger search
- `Tab`: Navigate between controls

## 🎨 Customization

### Styling
The visualizer uses Tailwind CSS for styling. Key customization points:

- **Color Scheme**: Modify `nodeColors` object in `App.tsx`
- **Layout**: Adjust CSS classes for responsive design
- **Animations**: Customize transition durations and effects

### Configuration
- **API Endpoints**: Update proxy settings in `package.json`
- **Refresh Intervals**: Modify available intervals in the auto-refresh component
- **Graph Settings**: Adjust force graph parameters for different layouts

## 🔧 Development

### Project Structure
```
src/
├── App.tsx              # Main application component
├── App.css              # Global styles and animations
├── index.tsx            # Application entry point
└── index.css            # Base styles
```

### Key Components
- **ForceGraph2D**: Main graph visualization component
- **Search Controls**: Search and filter interface
- **Statistics Panel**: Graph metrics display
- **Sidebar**: Detailed information panel
- **Info Overlay**: Real-time graph statistics

### Dependencies
- **react-force-graph-2d**: 2D force-directed graph visualization
- **axios**: HTTP client for API communication
- **tailwindcss**: Utility-first CSS framework
- **typescript**: Type safety and development experience

## 🐛 Troubleshooting

### Common Issues
1. **Graph Not Loading**: Check API endpoint configuration and network connectivity
2. **Performance Issues**: Reduce graph size or adjust rendering parameters
3. **Styling Issues**: Ensure Tailwind CSS is properly configured
4. **Mobile Issues**: Test responsive design on different screen sizes

### Debug Mode
Enable browser developer tools to view:
- Network requests and responses
- Component state and props
- Performance metrics
- Console errors and warnings

## 🔮 Future Enhancements

### Planned Features
- **3D Visualization**: Support for 3D graph rendering
- **Advanced Analytics**: More detailed graph metrics and insights
- **Export Functionality**: Export graphs as images or data files
- **Collaborative Features**: Real-time multi-user editing
- **Advanced Search**: Semantic search and natural language queries

### Performance Improvements
- **Virtual Scrolling**: Handle very large graphs efficiently
- **WebGL Rendering**: Hardware-accelerated rendering for better performance
- **Caching**: Intelligent caching of graph data and calculations

## 📄 License

This project is part of the EmiAi system and follows the same licensing terms.

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📞 Support

For issues and questions:
1. Check the troubleshooting section
2. Review the API documentation
3. Open an issue in the repository
4. Contact the development team 