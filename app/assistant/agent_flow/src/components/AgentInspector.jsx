import React, { useState, useEffect } from 'react';

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

export default function AgentInspector({
  selectedAgentId,
  config,
  onChange,
  allAgentConfigs,
  onOpenSchemaBuilder,
  availableAgents = []
}) {
  const [localConfig, setLocalConfig] = useState(config);
  const [saveStatus, setSaveStatus] = useState(null);
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [editingWhichPrompt, setEditingWhichPrompt] = useState('system');

  useEffect(() => {
    setLocalConfig(config);
    setSaveStatus(null);
  }, [config]);

  if (!localConfig) return null;

  const updateField = (path, value) => {
    const parts = path.split('.');
    const updated = { ...localConfig };
    let obj = updated;
    for (let i = 0; i < parts.length - 1; i++) {
      obj[parts[i]] = { ...obj[parts[i]] };
      obj = obj[parts[i]];
    }
    obj[parts.at(-1)] = value;
    setLocalConfig(updated);
  };

  const handleSave = () => {
    if (!selectedAgentId || !localConfig) return;
    onChange?.(localConfig);
    setSaveStatus('applied');
  };

  const togglePrompt = () => {
    setEditingWhichPrompt(prev => (prev === 'system' ? 'user' : 'system'));
  };

  const {
    name,
    class_name,
    color,
    llm_params = {},
    allowed_nodes = [],
    user_context_items = [],
    system_context_items = [],
    rag_fields = {},
    append_fields = [],
    prompts = { system: '', user: '' }
  } = localConfig;

  return (
    <>
      <div className="text-sm text-gray-800">
        <Section title="Basic Info">
          <div className="mb-2">
            <label className="block font-medium">Name:</label>
            <input
              className="border p-1 rounded w-full"
              value={name}
              onChange={e => updateField('name', e.target.value)}
            />
          </div>
          <div className="mb-2">
            <label className="block font-medium">Class:</label>
            <input
              className="border p-1 rounded w-full"
              value={class_name}
              onChange={e => updateField('class_name', e.target.value)}
            />
          </div>
          <div className="mb-2">
            <label className="block font-medium">Color:</label>
            <input
              className="border p-1 rounded w-full"
              value={color}
              onChange={e => updateField('color', e.target.value)}
            />
          </div>
        </Section>

        <Section title="LLM Params">
          {Object.entries(llm_params).map(([k, v]) => (
            <div key={k} className="mb-2">
              <label className="block font-medium">{k}:</label>
              <input
                className="border p-1 rounded w-full"
                value={v}
                onChange={e => updateField(`llm_params.${k}`, e.target.value)}
              />
            </div>
          ))}
        </Section>

        <Section title="Allowed Agents">
          <div className="ml-2">
            {availableAgents.map(({ name: agentName }) => (
              <label key={agentName} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={allowed_nodes.includes(agentName)}
                  onChange={e => {
                    const list = new Set(allowed_nodes);
                    if (e.target.checked) list.add(agentName);
                    else list.delete(agentName);
                    updateField('allowed_nodes', [...list]);
                  }}
                />
                {agentName}
              </label>
            ))}
          </div>
        </Section>

        <Section title="User Context Items">
          {user_context_items.map((item, idx) => (
            <div key={idx} className="flex gap-2 mb-1">
              <input
                className="border p-1 rounded w-full"
                value={item}
                onChange={e => {
                  const newList = [...user_context_items];
                  newList[idx] = e.target.value;
                  updateField('user_context_items', newList);
                }}
              />
              <button
                onClick={() =>
                  updateField(
                    'user_context_items',
                    user_context_items.filter((_, i) => i !== idx)
                  )
                }
                className="text-red-600"
              >
                ✕
              </button>
            </div>
          ))}
          <button
            onClick={() =>
              updateField('user_context_items', [...user_context_items, ''])
            }
            className="bg-blue-100 px-2 py-1 rounded text-sm"
          >
            + Add
          </button>
        </Section>

        <Section title="System Context Items">
          {system_context_items.map((item, idx) => (
            <div key={idx} className="flex gap-2 mb-1">
              <input
                className="border p-1 rounded w-full"
                value={item}
                onChange={e => {
                  const newList = [...system_context_items];
                  newList[idx] = e.target.value;
                  updateField('system_context_items', newList);
                }}
              />
              <button
                onClick={() =>
                  updateField(
                    'system_context_items',
                    system_context_items.filter((_, i) => i !== idx)
                  )
                }
                className="text-red-600"
              >
                ✕
              </button>
            </div>
          ))}
          <button
            onClick={() =>
              updateField('system_context_items', [...system_context_items, ''])
            }
            className="bg-blue-100 px-2 py-1 rounded text-sm"
          >
            + Add
          </button>
        </Section>

        <Section title="Prompts">
          <div className="mb-2">
            <button
              onClick={onOpenSchemaBuilder}
              className="bg-yellow-400 text-black px-2 py-1 rounded text-sm hover:bg-yellow-300"
            >
              ⚙️ Define Output Schema
            </button>
          </div>
          <div className="mb-2">
            <label className="block font-medium">System Prompt:</label>
            <textarea
              className="w-full text-xs p-2 border rounded bg-gray-50"
              rows={6}
              value={prompts.system}
              onChange={e => updateField('prompts.system', e.target.value)}
            />
          </div>
          <div>
            <label className="block font-medium">User Prompt:</label>
            <textarea
              className="w-full text-xs p-2 border rounded bg-gray-50"
              rows={6}
              value={prompts.user}
              onChange={e => updateField('prompts.user', e.target.value)}
            />
          </div>
          <div className="mt-2">
            <button
              onClick={() => {
                setEditingWhichPrompt('system');
                setEditingPrompt(true);
              }}
              className="bg-blue-600 text-white px-3 py-1 rounded text-sm hover:bg-blue-500"
            >
              ✏️ Edit Prompts in Fullscreen
            </button>
          </div>
        </Section>

        <button
          onClick={handleSave}
          className="mt-4 px-4 py-2 text-white rounded border"
          style={{
            backgroundColor: '#2563eb',
            borderColor: '#1d4ed8',
            color: 'white',
          }}
        >
          Save
        </button>

        {saveStatus === 'applied' && (
          <p className="text-green-600 mt-2">Changes applied to manager config.</p>
        )}
        <button
          onClick={() => setLocalConfig(config)}
          className="px-4 py-2 mt-2 bg-gray-300 rounded hover:bg-gray-400"
        >
          Cancel
        </button>
      </div>

      {editingPrompt && (
        <div className="fixed inset-0 z-50 bg-black bg-opacity-50 flex items-center justify-center">
          <div className="bg-white w-[90vw] h-[90vh] p-6 rounded shadow-lg flex flex-col">
            <div className="flex items-center justify-between mb-2">
              <button onClick={togglePrompt} className="text-2xl px-3">
                ◀
              </button>
              <h2 className="text-xl font-semibold">
                {editingWhichPrompt === 'system' ? 'System Prompt' : 'User Prompt'}
              </h2>
              <button onClick={togglePrompt} className="text-2xl px-3">
                ▶
              </button>
            </div>
            <textarea
              className="flex-1 resize-none border p-2 rounded bg-gray-50 text-sm"
              value={editingWhichPrompt === 'system' ? prompts.system : prompts.user || ''}
              onChange={(e) =>
                updateField(`prompts.${editingWhichPrompt}`, e.target.value)
              }
            />
            <div className="flex justify-end mt-4">
              <button
                className="px-4 py-2 bg-gray-300 rounded hover:bg-gray-400"
                onClick={() => setEditingPrompt(false)}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
