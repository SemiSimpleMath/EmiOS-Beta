import React, { useState, useEffect } from 'react';

function AddAgentModal({ allAgentConfigs, onClose, onAdd, currentManagerName }) {
  const [templateName, setTemplateName] = useState('');
  const [newAgentName, setNewAgentName] = useState('');

  const agentTemplates = Object.entries(allAgentConfigs).filter(
    ([, config]) => config.type === 'agent'
  );
  const controlNodeTemplates = Object.entries(allAgentConfigs).filter(
    ([, config]) => config.type === 'control_node'
  );

  useEffect(() => {
    const allTemplates = [...agentTemplates, ...controlNodeTemplates];
    if (allTemplates.length > 0 && !templateName) {
      setTemplateName(allTemplates[0][0]); // the key (templateName)
    }
  }, [allAgentConfigs]);

  const handleSubmit = () => {
    if (!templateName || !newAgentName) return;
    onAdd(newAgentName, templateName);
    onClose();
  };


  return (
    <div className="fixed inset-0 bg-black bg-opacity-40 z-50 flex items-center justify-center">
      <div className="bg-white rounded shadow-lg p-6 w-[400px]">
        <h2 className="text-xl font-bold mb-4">Add New Agent</h2>

        <label className="block mb-2 font-medium">Choose Template:</label>
        <select
          className="w-full border p-2 rounded mb-4"
          value={templateName}
          onChange={(e) => setTemplateName(e.target.value)}
        >
        <optgroup label="Agents">
          {agentTemplates.map(([templateName, config]) => (
            <option key={templateName} value={templateName}>
              {config.name}
            </option>
          ))}
        </optgroup>

        <optgroup label="Control Nodes">
          {controlNodeTemplates.map(([templateName, config]) => (
            <option key={templateName} value={templateName}>
              {config.name}
            </option>
          ))}
        </optgroup>

        </select>


        <label className="block mb-2 font-medium">
          New Agent Name: 
          {currentManagerName && (
            <span className="text-sm text-gray-500 ml-2">
              (will be prefixed with "{currentManagerName}::")
            </span>
          )}
        </label>
        <input
          className="w-full border p-2 rounded mb-4"
          placeholder={currentManagerName ? `e.g., planner` : `e.g., ${currentManagerName}::planner`}
          value={newAgentName}
          onChange={(e) => setNewAgentName(e.target.value)}
        />

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-4 py-2 bg-gray-300 rounded hover:bg-gray-400"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Add
          </button>
        </div>
      </div>
    </div>
  );
}

export default AddAgentModal;
