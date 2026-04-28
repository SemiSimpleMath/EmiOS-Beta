// src/components/AddManagerModal.jsx
import React, { useState } from 'react';

function AddManagerModal({ availableTemplates, onClose, onAdd }) {
  const [templateId, setTemplateId] = useState('');
  const [newManagerName, setNewManagerName] = useState('');

  const handleSubmit = () => {
    if (!templateId || !newManagerName) return;
    onAdd(newManagerName, templateId);
    onClose();
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-40 z-50 flex items-center justify-center">
      <div className="bg-white rounded shadow-lg p-6 w-[400px]">
        <h2 className="text-xl font-bold mb-4">Add New Manager</h2>

        <label className="block mb-2 font-medium">Choose Template:</label>
        <select
          className="w-full border p-2 rounded mb-4"
          value={templateId}
          onChange={e => setTemplateId(e.target.value)}
        >
          <option value="">— pick a template —</option>
          {availableTemplates.map(t => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>

        <label className="block mb-2 font-medium">New Manager Name:</label>
        <input
          className="w-full border p-2 rounded mb-4"
          placeholder="e.g., team_planner_manager"
          value={newManagerName}
          onChange={(e) => setNewManagerName(e.target.value)}
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

export default AddManagerModal;
