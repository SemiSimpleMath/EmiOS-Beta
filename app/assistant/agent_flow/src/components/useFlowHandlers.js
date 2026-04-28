import { useCallback } from 'react';
import { addEdge, MarkerType } from 'reactflow';

export default function useFlowHandlers({
  selectedEdge,
  isDraggingEdge,
  setEdges,
  setNodes,
  setIsDraggingEdge,
  setEdgeUpdateJustEnded,
}) {
  const handleConnect = useCallback((params) => {
    if (selectedEdge && !isDraggingEdge) return;
    setEdges((eds) =>
      addEdge(
        {
          ...params,
          markerEnd: {
            type: MarkerType.Arrow,
            width: 12,
            height: 12,
            color: '#232426',
          },
          style: {
            strokeWidth: 2,
            stroke: '#232426',
          },
        },
        eds
      )
    );
  }, [selectedEdge, isDraggingEdge, setEdges]);


    const handleEdgeUpdateStart = useCallback((event, edge) => {
      if (selectedEdge?.id !== edge.id) {
        console.warn('Only selected edge is editable');
        return false;
      }
      setIsDraggingEdge(true);
      setNodes((nodes) =>
        nodes.map((node) => ({
          ...node,
          data: {
            ...node.data,
            disableHandles:
              selectedEdge && (node.id === selectedEdge.source || node.id === selectedEdge.target)
                ? false
                : node.data.disableHandles,
            isEdgeUpdating: true,
          },
        }))
      );
      return true;
    }, [selectedEdge, setIsDraggingEdge, setNodes]);


    const handleEdgeUpdateEnd = useCallback(() => {
      setIsDraggingEdge(false);
      setNodes((nodes) =>
        nodes.map((node) => ({
          ...node,
          data: {
            ...node.data,
            disableHandles: false,
            isEdgeUpdating: false,
          },
        }))
      );
      // ✅ Prevents ghost connections immediately after drag ends
      setEdgeUpdateJustEnded(true);
      setTimeout(() => setEdgeUpdateJustEnded(false), 0);
    }, [setIsDraggingEdge, setNodes, setEdgeUpdateJustEnded]);

return {
  handleConnect,
  handleEdgeUpdateStart,
  handleEdgeUpdateEnd
};
}
