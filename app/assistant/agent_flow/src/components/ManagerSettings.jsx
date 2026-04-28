import React, { useState } from 'react';

function Section({ title, children }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mb-4">
      <button
        onClick={() => setOpen(!open)}
        className="w-full text-left font-semibold text-lg py-1 border-b border-gray-300"
      >
        {open ? '▼' : '▶'} {title}
      </button>
      {open && <div className="mt-2 pl-2">{children}</div>}
    </div>
  );
}

function ManagerSettings({ settings, onChange, onClose }) {
  if (!settings) return null;
  const [localSettings, setLocalSettings] = useState(settings);

  const updateField = (path, value) => {
    const parts = path.split('.');
    const updated = { ...localSettings };
    let obj = updated;
    for (let i = 0; i < parts.length - 1; i++) {
      obj[parts[i]] = { ...obj[parts[i]] };
      obj = obj[parts[i]];
    }
    obj[parts.at(-1)] = value;
    setLocalSettings(updated);
    if (onChange) onChange(updated);
  };

  return (
    <div className="absolute top-0 left-0 w-[400px] h-full bg-white shadow-lg z-50 overflow-y-auto p-4">
      <div className="flex justify-between items-center">
        <h2 className="text-xl font-bold">Manager Settings</h2>
        <button
          onClick={onClose}
          className="text-gray-500 hover:text-red-600 text-lg"
        >
          ✕
        </button>
      </div>

      <Section title="Basic Info">
        <div className="mb-2">
          <label className="block font-medium">Name:</label>
          <input
            className="border p-1 rounded w-full"
            value={localSettings.name}
            onChange={(e) => updateField('name', e.target.value)}
          />
        </div>
        <div className="mb-2">
          <label className="block font-medium">Description:</label>
          <textarea
            className="border p-1 rounded w-full"
            rows={3}
            value={localSettings.description || ''}
            onChange={(e) => updateField('description', e.target.value)}
          />
        </div>
        <div className="mb-2">
          <label className="block font-medium">Max Cycles:</label>
          <input
            type="number"
            className="border p-1 rounded w-full"
            value={localSettings.max_cycles || 10}
            onChange={(e) => updateField('max_cycles', parseInt(e.target.value))}
          />
        </div>
      </Section>

      <Section title="Tools">
        <label className="block font-medium mb-1">Allowed Tools:</label>
        <textarea
          className="w-full p-1 border rounded mb-2"
          rows={2}
          value={(localSettings.tools?.allowed_tools || []).join('\n')}
          onChange={(e) => updateField('tools.allowed_tools', e.target.value.split('\n'))}
        />
        <label className="block font-medium mb-1">Except Tools:</label>
        <textarea
          className="w-full p-1 border rounded"
          rows={2}
          value={(localSettings.tools?.except_tools || []).join('\n')}
          onChange={(e) => updateField('tools.except_tools', e.target.value.split('\n'))}
        />
      </Section>

      <Section title="Events">
        <textarea
          className="w-full p-1 border rounded"
          rows={2}
          value={(localSettings.events || []).join('\n')}
          onChange={(e) => updateField('events', e.target.value.split('\n'))}
        />
      </Section>
    </div>
  );
}

export default ManagerSettings;
