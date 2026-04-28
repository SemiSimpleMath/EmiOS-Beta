import React, { useEffect, useState, useRef, useMemo } from 'react';
import { useReactFlow } from 'reactflow';
import ReactFlow, {
  addEdge,
  useNodesState,
  useEdgesState,
  Background,
  Controls,
  MarkerType,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { applyLayout } from '../utils/layout';
import CustomNode from './CustomNode';
import AgentInspector from './AgentInspector';
import AddAgentModal from './AddAgentModal';
import AddManagerModal from './AddManagerModal';
import SchemaBuilderModal from './SchemaBuilderModal';
import ManagerSettings from './ManagerSettings';
import { v4 as uuidv4 } from 'uuid';
import useFlowHandlers from './useFlowHandlers';

function updateAgentConfigInManager(setManagers, currentManagerId, agentId, updatedConfig) {

  setManagers((prev) => {
    console.log(`Updating config for agent ${agentId} in manager ${currentManagerId}`, updatedConfig);

    return {
      ...prev,
      [currentManagerId]: {
        ...prev[currentManagerId],
        agents: {
          ...prev[currentManagerId].agents,
          [agentId]: updatedConfig,
        },
      },
    };
  });
}


function updateManagerSettingsInManager(setManagers, currentManagerId, updatedSettings) {
  setManagers((prev) => ({
    ...prev,
    [currentManagerId]: {
      ...prev[currentManagerId],
      manager_config: updatedSettings,
    },
  }));
}

function FlowEditor() {
  // New state for managing all managers
  const [managers, setManagers] = useState({});
  const [currentManagerId, setCurrentManagerId] = useState('emi_team_manager');

  // Removed separate managerSettings state – we now refer directly to managers[currentManagerId].manager_config
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  const [loading, setLoading] = useState(false);
  const [managersLoaded, setManagersLoaded] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState(null);
  const [agentConfig, setAgentConfig] = useState(null);
  const [selectedEdge, setSelectedEdge] = useState(null);
  const [isDraggingEdge, setIsDraggingEdge] = useState(false);
  const [edgeUpdateJustEnded, setEdgeUpdateJustEnded] = useState(false);
  const [allAgentConfigs, setAllAgentConfigs] = useState({});
  const [showAddModal, setShowAddModal] = useState(false);
  const [contextMenu, setContextMenu] = useState(null);
  const [showSchemaBuilder, setShowSchemaBuilder] = useState(false);
  const [showManagerSettings, setShowManagerSettings] = useState(false);
  const [isInspectorOpen, setIsInspectorOpen] = useState(false);
  const [showAddManagerModal, setShowAddManagerModal] = useState(false);
  const [availableManagerTemplates, setAvailableManagerTemplates] = useState([]);

  const reactFlowInstanceRef = useRef(null);

  const {
    handleConnect,
    handleEdgeUpdateStart,
    handleEdgeUpdateEnd,
  } = useFlowHandlers({
    selectedEdge,
    isDraggingEdge,
    setEdges,
    setNodes,
    setIsDraggingEdge,
    setEdgeUpdateJustEnded,
  });

      const memoizedNodeTypes = useMemo(() => ({
        default: CustomNode,
      }), []);



  // Load all managers with their configs, agents, and flow_config.
    useEffect(() => {
      const loadAllManagers = async () => {
        try {
          const res = await fetch('http://localhost:8000/agent_flow/get_all_manager_configs');
          const json = await res.json();
          setManagers(json);
          setCurrentManagerId(Object.keys(json)[0]);
          setManagersLoaded(true); // <-- ✅ this
        } catch (err) {
          console.error('Failed to load all managers:', err);
        }
      };
      loadAllManagers();
    }, []);


useEffect(() => {
  if (!managers[currentManagerId]) return;
  if (!reactFlowInstanceRef.current) return;

  const manager = managers[currentManagerId];
  setLoading(true);

  // Always apply layout on manager change
  const laidOutNodes = applyLayout(manager.flow.nodes, manager.flow.edges, 'TB');

  // Compute bounding box center of the laid-out nodes
  const minX = Math.min(...laidOutNodes.map(n => n.position.x));
  const maxX = Math.max(...laidOutNodes.map(n => n.position.x));
  const minY = Math.min(...laidOutNodes.map(n => n.position.y));
  const maxY = Math.max(...laidOutNodes.map(n => n.position.y));
  const offsetX = (minX + maxX) / 2;
  const offsetY = (minY + maxY) / 2;

  // Use the window dimensions as the visible screen center
  const centerX = window.innerWidth / 2;
  const centerY = window.innerHeight / 2;

  // Shift nodes so that the bounding box center lines up with the screen center
  const scale = 0.8; // 0.8 = zoomed out a bit
  const centeredNodes = laidOutNodes.map(n => ({
    ...n,
    position: {
        x: (n.position.x - offsetX) * scale + centerX,
        y: (n.position.y - offsetY) * scale + centerY,
    }
  }));

  const preparedNodes = centeredNodes.map(n => ({
    id: n.id,
    type: 'default',
    position: n.position,
    data: {
      label: n.data?.label ?? n.label ?? n.id,
      type: n.data?.type ?? n.type,
      disableHandles: false,
    },
    style: {
      border: 'none',
      background: 'transparent',
      boxShadow: 'none',
    },
  }));


    const styledEdges = (manager.flow.edges || []).map(e => {
      const id = e.id || `${e.source}-${e.target}`;
      const isSelected = selectedEdge?.id === id;

      let stroke = isSelected ? '#f97316' : '#232426';
      let strokeDasharray = 'none';
      let markerEndColor = isSelected ? '#f97316' : '#232426';

      if (e.edge_type === 'allowed') {
        stroke = '#9999ff';
        strokeDasharray = '4 2';
        markerEndColor = '#9999ff';
      } else if (e.edge_type === 'tool_edge') {
        stroke = isSelected ? '#34d399' : '#10b981';         // green-ish for tool edges
        strokeDasharray = '6 3';                              // custom dash pattern for tool edges
        markerEndColor = isSelected ? '#34d399' : '#10b981';
      }

      return {
        id,
        source: e.source,
        target: e.target,
        updatable: isSelected,
        markerEnd: {
          type: MarkerType.Arrow,
          width: 12,
          height: 12,
          color: markerEndColor,
        },
        style: {
          stroke,
          strokeDasharray,
          strokeWidth: isSelected ? 3 : 2,
        },
        edge_type: e.edge_type,
      };
    });


  setNodes(preparedNodes);
  setEdges(styledEdges);
  setLoading(false);
}, [currentManagerId, managersLoaded]);




  // Update the active manager's flow whenever nodes or edges change.
  useEffect(() => {
    if (!currentManagerId || !managers[currentManagerId]) return;
    setManagers((prev) => ({
      ...prev,
      [currentManagerId]: {
        ...prev[currentManagerId],
        flow: {
          ...prev[currentManagerId].flow,
          nodes,
          edges,
          layouted: true, // mark layout as manually set
        },
      },
    }));
  }, [nodes, edges, currentManagerId]);


  useEffect(() => {
    setNodes((nodes) =>
      nodes.map((node) => {
        const disable =
          selectedEdge && !isDraggingEdge
            ? node.id !== selectedEdge.source && node.id !== selectedEdge.target
            : false;
        return {
          ...node,
          data: {
            ...node.data,
            disableHandles: disable,
            isEdgeUpdating: isDraggingEdge,
          },
        };
      })
    );
  }, [selectedEdge, isDraggingEdge]);

  useEffect(() => {
    if (selectedAgentId && managers[currentManagerId]?.agents?.[selectedAgentId]) {
      setAgentConfig(managers[currentManagerId].agents[selectedAgentId]);
    }
  }, [selectedAgentId, currentManagerId, managers]);

  useEffect(() => {
    let retryDelay = 500;
    let maxDelay = 8000;

    const fetchTemplates = async () => {
      try {
        const res = await fetch('http://localhost:8000/agent_flow/all_agent_configs');
        if (!res.ok) {
          if (res.status === 503) {
            console.warn(`Agent registry not ready, retrying in ${retryDelay}ms...`);
            setTimeout(fetchTemplates, retryDelay);
            retryDelay = Math.min(retryDelay * 2, maxDelay);
          } else {
            throw new Error('Failed to load agent configs');
          }
          return;
        }
        const json = await res.json();
        setAllAgentConfigs(json);
      } catch (err) {
        console.error('Agent config fetch failed:', err);
      }
    };

    fetchTemplates();
  }, []);

  const templateOptions = useMemo(() => {
    return Object.entries(managers).map(([id, mgr]) => ({
      id,
      name: mgr.manager_config?.name || id,
    }));
  }, [managers]);

  const handleEdgeClick = (event, edge) => {
    event.stopPropagation();
    setSelectedEdge((prev) => (prev?.id === edge.id ? null : edge));
  };

  const handleEdgeUpdate = (oldEdge, newConnection) => {
    if (!newConnection.source || !newConnection.target) {
      console.warn('Invalid connection', newConnection);
      setIsDraggingEdge(false);
      return;
    }
    setEdges((eds) =>
      eds.map((e) =>
        e.id === oldEdge.id
          ? {
              ...e,
              source: newConnection.source,
              target: newConnection.target,
              sourceHandle: newConnection.sourceHandle,
              targetHandle: newConnection.targetHandle,
            }
          : e
      )
    );
    setIsDraggingEdge(false);
  };

  const handleEdgesDelete = (deleted) => {
    // If we have a selected edge and it's being deleted, clear the selection
    if (selectedEdge && deleted.some(d => d.id === selectedEdge.id)) {
      setSelectedEdge(null);
    }
    
    // Remove the deleted edges
    setEdges((eds) => eds.filter((e) => !deleted.some((d) => d.id === e.id)));
  };

    useEffect(() => {
      setEdges((eds) =>
        eds.map((e) => {
          const isSelected = selectedEdge?.id === e.id;
          return {
            ...e,
            updatable: isSelected,
            style: {
              ...e.style,
              stroke:
                e.edge_type === 'allowed'
                  ? '#9999ff' // light blue for allowed edges
                  : isSelected
                  ? '#f97316'
                  : '#232426',
              strokeWidth: isSelected ? 3 : 2,
            },
            markerEnd: {
              ...e.markerEnd,
              color:
                e.edge_type === 'allowed'
                  ? '#9999ff'
                  : isSelected
                  ? '#f97316'
                  : '#232426',
            },
          };
        })
      );
    }, [selectedEdge]);



  const handleDeleteAgent = (agentId) => {
    setNodes((nds) => nds.filter((n) => n.id !== agentId));
    setEdges((eds) => eds.filter((e) => e.source !== agentId && e.target !== agentId));
    setManagers((prev) => {
      const updated = { ...prev };
      delete updated[currentManagerId].agents[agentId];
      return updated;
    });

    if (selectedAgentId === agentId) {
      setSelectedAgentId(null);
      setAgentConfig(null);
    }
  };

  const handleDuplicateAgent = (agentId) => {
    const originalConfig = managers[currentManagerId]?.agents?.[agentId];
    if (!originalConfig) return;

    // Generate a new UUID for the duplicated agent
    const newId = uuidv4();
    const newConfig = {
      ...JSON.parse(JSON.stringify(originalConfig)),
      name: `${originalConfig.name}_copy`,
    };

    const newNode = {
      id: newId,
      type: 'default',
      position: { x: 100, y: 100 },
      data: {
        label: newConfig.name,
        type: newConfig.type || 'agent',
        disableHandles: false,
      },
      style: {
        border: 'none',
        background: 'transparent',
        boxShadow: 'none',
      },
    };

    setNodes((nds) => [...nds, newNode]);
    updateAgentConfigInManager(setManagers, currentManagerId, newId, newConfig);
  };

  const handleFlowInit = (reactFlowInstance) => {
    reactFlowInstanceRef.current = reactFlowInstance;
  };

  const handleSaveManager = async () => {
    const flow = { nodes, edges };

        const idToName = Object.fromEntries(
          nodes.map((n) => [n.id, n.data?.label || n.id])
        );

        const stateMap = {};
        for (const edge of edges) {
          if (edge.edge_type === 'flow') {
            const sourceName = idToName[edge.source];
            const targetName = idToName[edge.target];
            if (sourceName && targetName) {
              stateMap[sourceName] = targetName;
            }
          }
        }


  try {
    const res = await fetch('http://localhost:8000/agent_flow/save_manager', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manager_config: {
          ...managers[currentManagerId].manager_config,
          flow_config: { state_map: stateMap },
        },
        agents: managers[currentManagerId].agents,
        flow,
      }),
    });

      if (!res.ok) throw new Error('Save failed');
      console.log('✅ Manager saved!');
      setManagers((prev) => ({
        ...prev,
        [currentManagerId]: {
          ...prev[currentManagerId],
          flow,
          saved: true,
        },
      }));
    } catch (err) {
      console.error('❌ Failed to save manager:', err);
    }
  };
  const availableAgents = Object.entries(managers[currentManagerId]?.agents || {})
    .filter(([id]) => id !== selectedAgentId)
    .map(([id, cfg]) => ({
      id,
      name: cfg.name,
    }));
  return (
    <div className="w-screen h-screen relative">
      <div className="absolute top-80 left-2 z-10 flex items-center gap-2 bg-white p-2 rounded shadow">
        <button
          className="bg-blue-500 text-black px-2 py-1 rounded"
          onClick={() => setShowAddManagerModal(true)}
        >
          + Add Manager
        </button>
        {loading && <span className="text-gray-700">Loading...</span>}
        <button
          className="bg-blue-500 text-black px-2 py-1 rounded"
          onClick={() => setShowAddModal(true)}
        >
          + Add Agent
        </button>
      </div>

      <div className="absolute top-2 left-2 z-10 flex flex-col gap-2 bg-white p-2 rounded shadow">
        <button
          className="bg-yellow-500 text-black px-2 py-1 rounded"
          onClick={() => setShowManagerSettings(true)}
        >
          ⚙️ Manager Settings
        </button>
        <h1 className="text-2xl font-bold text-gray-800 truncate">
          {managers[currentManagerId]?.manager_config?.name || currentManagerId}
        </h1>
      </div>

      <ReactFlow
        onInit={handleFlowInit}
        key={currentManagerId}
        nodes={nodes}
        edges={edges}
        nodeTypes={memoizedNodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={(e, node) => {
          const agentId = node.id;
          setSelectedAgentId(agentId);
          let config = managers[currentManagerId]?.agents?.[agentId];
          if (!config) {
            console.warn('Agent config not found for', agentId);
            config = { name: agentId };
          }
          setAgentConfig(config);
        }}
        onNodeDoubleClick={(e, node) => {
          setSelectedAgentId(node.id);
          setIsInspectorOpen(true);
        }}
        onConnect={handleConnect}
        onEdgeClick={handleEdgeClick}
        onEdgeUpdate={handleEdgeUpdate}
        onEdgeUpdateStart={handleEdgeUpdateStart}
        onEdgeUpdateEnd={handleEdgeUpdateEnd}
        onEdgesDelete={handleEdgesDelete}
        connectionLineStyle={{ stroke: '#f97316', strokeWidth: 2 }}
        snapToGrid
        snapGrid={[15, 15]}
        isValidConnection={(connection) => {
          if (edgeUpdateJustEnded) return false;
          if (!selectedEdge) return true;
          return (
            connection.source === selectedEdge.source ||
            connection.source === selectedEdge.target ||
            connection.target === selectedEdge.source ||
            connection.target === selectedEdge.target
          );
        }}
        onConnectStart={(event, params) => {
          if (edgeUpdateJustEnded) {
            event.preventDefault();
            return false;
          }
          if (selectedEdge) {
            if (
              params.source !== selectedEdge.source &&
              params.source !== selectedEdge.target
            ) {
              event.preventDefault();
              return false;
            }
          }
        }}
        deleteKeyCode={['Backspace', 'Delete']}
        onNodeContextMenu={(event, node) => {
          event.preventDefault();
          setContextMenu({
            node,
            x: event.clientX,
            y: event.clientY,
          });
        }}
      >
        <Background />
        <Controls />
      </ReactFlow>

      <div style={{ bottom: '200px', left: '8px' }} className="flex items-center gap-2 absolute z-10">
        <label htmlFor="manager-select" className="text-sm font-medium text-gray-700">
          Current Manager:
        </label>
        <select
          id="manager-select"
          value={currentManagerId}
          onChange={(e) => setCurrentManagerId(e.target.value)}
          className="px-2 py-1 border rounded shadow text-sm"
        >
          {Object.keys(managers).map((managerId) => (
            <option key={managerId} value={managerId}>
              {managers[managerId]?.manager_config?.name || managerId}
            </option>
          ))}
        </select>
      </div>

      <div className="absolute bottom-20 left-2 z-10">
        <button
          className="bg-green-600 text-white px-3 py-1 rounded shadow hover:bg-green-700"
          onClick={handleSaveManager}
          title="Save all agents + graph layout"
        >
          💾 Save Manager
        </button>
      </div>

      {contextMenu && (
        <div
          className="absolute bg-white border rounded shadow z-50 text-sm"
          style={{ top: contextMenu.y, left: contextMenu.x }}
          onMouseLeave={() => setContextMenu(null)}
        >
          <button
            className="block px-4 py-2 w-full text-left hover:bg-gray-100"
            onClick={() => {
              const nodeId = contextMenu.node.id;
              setNodes((nds) => nds.filter((n) => n.id !== nodeId));
              setEdges((eds) => eds.filter((e) => e.source !== nodeId && e.target !== nodeId));
              setSelectedAgentId(null);
              setContextMenu(null);
            }}
          >
            ❌ Delete Agent
          </button>
          <button
            className="block px-4 py-2 w-full text-left hover:bg-gray-100"
            onClick={() => {
              handleDuplicateAgent(contextMenu.node.id);
              setContextMenu(null);
            }}
          >
            📄 Duplicate Agent
          </button>
        </div>
      )}

      {showAddModal && (
        <AddAgentModal
          allAgentConfigs={allAgentConfigs}
          onClose={() => setShowAddModal(false)}
          currentManagerName={managers[currentManagerId]?.manager_config?.name}
          onAdd={(newAgentName, templateName) => {
            console.log('ADDING AGENT', newAgentName, templateName);
            const base = allAgentConfigs[templateName];
            if (!base) return;

            const newConfig = { ...JSON.parse(JSON.stringify(base)) };
            
            // PREFIX AGENT NAME WITH MANAGER NAMESPACE
            const currentManager = managers[currentManagerId];
            const managerName = currentManager?.manager_config?.name || currentManagerId;
            const managerNamespace = `${managerName}::`;
            
            // Only prefix if the user didn't already include a namespace
            if (!newAgentName.includes('::')) {
              newConfig.name = `${managerNamespace}${newAgentName}`;
            } else {
              newConfig.name = newAgentName;
            }

            const newNode = {
              id: newConfig.name, // Use the full name as ID
              type: 'default',
              position: { x: 100, y: 100 },
              data: {
                label: newConfig.name,
                type: newConfig.type || 'agent',
                disableHandles: false,
              },
              style: {
                border: 'none',
                background: 'transparent',
                boxShadow: 'none',
              },
            };

            setNodes((nodes) => [...nodes, newNode]);
            updateAgentConfigInManager(setManagers, currentManagerId, newConfig.name, newConfig);

            setAgentConfig(newConfig);
            setSelectedAgentId(newConfig.name);
          }}
        />
      )}

        {showAddManagerModal && (
          <AddManagerModal
            availableTemplates={templateOptions} // [{ id, name }]
            onClose={() => setShowAddManagerModal(false)}
            onAdd={(newManagerName, templateId) => {
              // 1️⃣ grab the template manager
              const template = managers[templateId];
              if (!template) return;

              // 2️⃣ deep‑clone the entire manager (config + agents + flow)
              const clone = JSON.parse(JSON.stringify(template));

              // 3️⃣ assign a fresh UUID and update its display name
              const newManagerId = uuidv4();
              clone.manager_config.id = newManagerId;
              clone.manager_config.name = newManagerName;

              // 4️⃣ PREFIX ALL AGENT NAMES WITH MANAGER NAMESPACE
              const managerNamespace = `${newManagerName}::`;
              
              // Update agent configs
              Object.keys(clone.agents).forEach(agentId => {
                const agentConfig = clone.agents[agentId];
                const originalName = agentConfig.name;
                
                // Only prefix if it doesn't already have a namespace
                if (!originalName.includes('::')) {
                  agentConfig.name = `${managerNamespace}${originalName}`;
                }
              });

              // Update flow edges to use new agent names
              if (clone.flow && clone.flow.edges) {
                clone.flow.edges.forEach(edge => {
                  // Find the agent configs for source and target
                  const sourceAgent = Object.values(clone.agents).find(a => a.agent_id === edge.source);
                  const targetAgent = Object.values(clone.agents).find(a => a.agent_id === edge.target);
                  
                  if (sourceAgent && targetAgent) {
                    // Update edge labels if needed
                    const sourceNode = clone.flow.nodes.find(n => n.id === edge.source);
                    const targetNode = clone.flow.nodes.find(n => n.id === edge.target);
                    
                    if (sourceNode) sourceNode.data.label = sourceAgent.name;
                    if (targetNode) targetNode.data.label = targetAgent.name;
                  }
                });
              }

              // Update flow config state_map if it exists
              if (clone.manager_config.flow_config && clone.manager_config.flow_config.state_map) {
                const newStateMap = {};
                Object.entries(clone.manager_config.flow_config.state_map).forEach(([source, target]) => {
                  const sourceAgent = Object.values(clone.agents).find(a => a.name === source || a.name.endsWith(`::${source}`));
                  const targetAgent = Object.values(clone.agents).find(a => a.name === target || a.name.endsWith(`::${target}`));
                  
                  if (sourceAgent && targetAgent) {
                    newStateMap[sourceAgent.name] = targetAgent.name;
                  }
                });
                clone.manager_config.flow_config.state_map = newStateMap;
              }

              // 5️⃣ insert the clone under its new UUID
              setManagers(prev => ({
                ...prev,
                [newManagerId]: clone,
              }));

              // 6️⃣ switch the view to the newly cloned manager
              setCurrentManagerId(newManagerId);
              setShowAddManagerModal(false);
            }}
          />
)}



      {showSchemaBuilder && (
        <SchemaBuilderModal
          onClose={() => setShowSchemaBuilder(false)}
          initialSchema={agentConfig}
          onSave={(data) => {
            const updated = { ...agentConfig };
            updated.schema_config = {
              ...updated.schema_config,  // preserve any existing schema config
              ...data.schema_config      // overwrite with new values
            };
            setAgentConfig(updated);
            updateAgentConfigInManager(setManagers, currentManagerId, selectedAgentId, updated);
          }}
        />

      )}

      {showManagerSettings && (
        <ManagerSettings
          settings={managers[currentManagerId].manager_config}
          onChange={(updated) =>
            updateManagerSettingsInManager(setManagers, currentManagerId, updated)
          }
          onClose={() => setShowManagerSettings(false)}
        />
      )}

      {isInspectorOpen && selectedAgentId && agentConfig && (
        <div className="absolute top-0 right-0 w-[400px] h-full bg-white shadow-lg z-50 overflow-y-auto p-4">
          <div className="flex justify-between items-center">
            <h2 className="text-xl font-bold">{agentConfig?.name || selectedAgentId}</h2>
            <button
              onClick={() => setIsInspectorOpen(false)}
              className="text-gray-500 hover:text-red-600 text-lg"
            >
              ✕
            </button>
          </div>

            <AgentInspector
              config={agentConfig}
              selectedAgentId={selectedAgentId}
              allAgentConfigs={allAgentConfigs}
              availableAgents={availableAgents}
onChange={(updated) => {
  setAgentConfig(updated);
  updateAgentConfigInManager(setManagers, currentManagerId, selectedAgentId, updated);

  // 👇 Remove outdated allowed edges
  setEdges((prevEdges) =>
    prevEdges.filter(
      (e) => !(e.source === selectedAgentId && e.edge_type === 'allowed')
    )
  );

  // 👇 Add new allowed edges
  const newAllowedEdges = (updated.allowed_nodes || []).map((targetName) => {
    const targetEntry = Object.entries(managers[currentManagerId].agents || {})
      .find(([_, cfg]) => cfg.name === targetName);
    if (!targetEntry) return null;
    const [targetId] = targetEntry;
    return {
      id: `allowed-${selectedAgentId}-${targetId}`,
      source: selectedAgentId,
      target: targetId,
      edge_type: 'allowed',
      style: {
        stroke: '#9999ff',
        strokeDasharray: '4 2',
      },
      markerEnd: {
        type: 'arrowclosed',
        color: '#9999ff',
      },
    };
  }).filter(Boolean);

  setEdges((prev) => [...prev, ...newAllowedEdges]);

  setNodes((nodes) =>
    nodes.map((n) =>
      n.id === selectedAgentId
        ? { ...n, data: { ...n.data, label: updated.name } }
        : n
    )
  );
}}

              onOpenSchemaBuilder={() => setShowSchemaBuilder(true)}
            />

        </div>
      )}
    </div>
  );
}

export default FlowEditor;
