import React, { useState, useRef, useEffect } from 'react';
import { Rnd } from 'react-rnd';

const DEFAULT_FIELD = {
  name: '',
  type: 'string',
  description: '',
  required: true,
  fields: [], // For nested object fields
};

const SCALAR_TYPES = ['string', 'number', 'integer', 'boolean', 'list_of_dicts', 'object'];

export default function SchemaBuilderModal({ onClose, onSave, initialSchema }) {
  // Mode state: 'json' or 'pydantic'
  const [mode, setMode] = useState('json');
  const [fields, setFields] = useState([{ ...DEFAULT_FIELD }]);
  const [editIndex, setEditIndex] = useState(0);
  const containerRef = useRef(null);
  // pydantic schema state with default skeleton code
  const [pydanticSchema, setPydanticSchema] = useState(
    `from pydantic import BaseModel

class AgentForm(BaseModel):
    place_holder: str`
  );

    useEffect(() => {
      if (initialSchema?.schema_config?.pydantic_code) {
        setPydanticSchema(initialSchema.schema_config.pydantic_code);
      } else {
        setPydanticSchema(`from pydantic import BaseModel\n\nclass AgentForm(BaseModel):\n    place_holder: str`);
      }
    }, [initialSchema]);


  useEffect(() => {
    if (initialSchema?.properties) {
      const restoredFields = Object.entries(initialSchema.properties).map(([name, prop]) => {
        const isListOfDicts =
          prop.type === 'array' &&
          prop.items?.type === 'object' &&
          prop.items?.properties?.key &&
          prop.items?.properties?.value;
        const isObject = prop.type === 'object' && prop.properties;
        return {
          name,
          type: isListOfDicts ? 'list_of_dicts' : isObject ? 'object' : prop.type || 'string',
          description: prop.description || '',
          required: initialSchema.required?.includes(name),
          fields: isObject
            ? Object.entries(prop.properties).map(([subName, subProp]) => ({
                name: subName,
                type: subProp.type || 'string',
                description: subProp.description || '',
                required: prop.required?.includes(subName),
              }))
            : [],
        };
      });
      setFields(restoredFields.length > 0 ? restoredFields : [{ ...DEFAULT_FIELD }]);
    }
  }, [initialSchema]);

  const updateField = (index, key, value) => {
    const updated = [...fields];
    if (key === 'type' && value !== 'object') {
      updated[index].fields = [];
    }
    updated[index][key] = value;
    setFields(updated);
  };

  const updateNestedField = (fieldIndex, subIndex, key, value) => {
    const updated = [...fields];
    if (!Array.isArray(updated[fieldIndex].fields)) {
      updated[fieldIndex].fields = [];
    }
    updated[fieldIndex].fields[subIndex][key] = value;
    setFields(updated);
  };

  const addField = () => {
    setFields([...fields, { ...DEFAULT_FIELD }]);
    setEditIndex(fields.length);
  };

  const addNestedField = (fieldIndex) => {
    const updated = [...fields];
    if (!Array.isArray(updated[fieldIndex].fields)) {
      updated[fieldIndex].fields = [];
    }
    updated[fieldIndex].fields.push({ name: '', type: 'string', description: '', required: true });
    setFields(updated);
  };

  const removeField = (index) => {
    setFields(fields.filter((_, i) => i !== index));
    if (editIndex === index) setEditIndex(null);
  };

  const removeNestedField = (fieldIndex, subIndex) => {
    const updated = [...fields];
    if (!Array.isArray(updated[fieldIndex].fields)) {
      return;
    }
    updated[fieldIndex].fields = updated[fieldIndex].fields.filter((_, i) => i !== subIndex);
    setFields(updated);
  };

  const buildSchema = () => {
    const properties = {};
    const required = [];
    for (const f of fields) {
      if (!f.name.trim()) continue;
      if (f.type === 'list_of_dicts') {
        properties[f.name] = {
          type: 'array',
          description: f.description,
          items: {
            type: 'object',
            properties: {
              key: { type: 'string', description: 'The key or identifier' },
              value: { type: 'string', description: 'The associated value' },
            },
            required: ['key', 'value'],
            additionalProperties: false,
          },
        };
      } else if (f.type === 'object') {
        const nestedProps = {};
        const nestedReqs = [];
        const nestedFields = Array.isArray(f.fields) ? f.fields : [];
        for (const sub of nestedFields) {
          if (!sub.name.trim()) continue;
          nestedProps[sub.name] = {
            type: sub.type,
            description: sub.description,
          };
          if (sub.required) nestedReqs.push(sub.name);
        }
        properties[f.name] = {
          type: 'object',
          description: f.description,
          properties: nestedProps,
          required: nestedReqs,
          additionalProperties: false,
        };
      } else {
        properties[f.name] = {
          type: f.type,
          description: f.description,
        };
      }
      if (f.required) required.push(f.name);
    }
    return {
      type: 'object',
      properties,
      required,
      additionalProperties: false,
    };
  };

  const schema = buildSchema();
  const lastFieldValid = fields[fields.length - 1]?.name?.trim();
  const allFieldsValid = fields.every(f => f.name.trim());

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [editIndex]);

  const handleSave = () => {
    if (mode === 'json') {
      onSave({ schema_config: { json_schema: schema } });
    } else if (mode === 'pydantic') {
      onSave({ schema_config: { pydantic_code: pydanticSchema } });
    }
  };

  return (
    <div className="fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-50 cursor-default">
      <Rnd
        default={{ x: 100, y: 100, width: 600, height: 600 }}
        minWidth={400}
        minHeight={400}
        bounds="window"
        enableResizing
        dragHandleClassName="handle"
      >
        <div className="bg-white p-6 rounded shadow-md w-full h-full flex flex-col relative">
          <button
            className="absolute top-2 right-2 text-gray-400 hover:text-red-600 text-lg"
            onClick={onClose}
            title="Close"
          >
            ✕
          </button>

          {/* Mode Toggle */}
          <div className="mb-4 flex gap-2">
            <button
              className={`px-3 py-1 rounded ${mode === 'json' ? 'bg-blue-600 text-white' : 'bg-gray-200'}`}
              onClick={() => setMode('json')}
            >
              JSON Schema
            </button>
            <button
              className={`px-3 py-1 rounded ${mode === 'pydantic' ? 'bg-blue-600 text-white' : 'bg-gray-200'}`}
              onClick={() => setMode('pydantic')}
            >
              Pydantic
            </button>
          </div>

          {mode === 'json' ? (
            <>
              <h2 className="text-lg font-semibold mb-4 handle">Define Output Schema</h2>
              <div className="flex-1 overflow-y-auto">
                {fields.map((field, i) => (
                  <div
                    key={i}
                    ref={i === editIndex ? containerRef : null}
                    className={`border p-2 mb-3 rounded bg-gray-50 ${i === editIndex ? 'ring-2 ring-blue-400' : ''}`}
                  >
                    <div className="flex gap-2 mb-1">
                      <input
                        className="border p-1 flex-1 cursor-text"
                        placeholder="Field name"
                        value={field.name}
                        onChange={(e) => updateField(i, 'name', e.target.value)}
                      />
                      <select
                        className="border p-1 cursor-pointer"
                        value={field.type}
                        onChange={(e) => updateField(i, 'type', e.target.value)}
                      >
                        {SCALAR_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                      <label className="flex items-center gap-1 cursor-pointer">
                        <input
                          type="checkbox"
                          checked={field.required}
                          onChange={(e) => updateField(i, 'required', e.target.checked)}
                        />
                        Required
                      </label>
                      <button
                        className="text-red-600 hover:text-red-800 ml-auto"
                        onClick={() => removeField(i)}
                      >
                        ✕
                      </button>
                    </div>
                    <textarea
                      className="border w-full p-1 cursor-text"
                      placeholder="Description"
                      value={field.description}
                      onChange={(e) => updateField(i, 'description', e.target.value)}
                    />

                    {field.type === 'object' && (
                      <div className="ml-4 mt-2 border-l pl-4">
                        <h4 className="font-medium text-sm mb-1">Nested Fields</h4>
                        {Array.isArray(field.fields) &&
                          field.fields.map((sub, j) => (
                            <div key={j} className="flex gap-2 mb-1">
                              <input
                                className="border p-1 flex-1 cursor-text"
                                placeholder="Nested field name"
                                value={sub.name}
                                onChange={(e) => updateNestedField(i, j, 'name', e.target.value)}
                              />
                              <select
                                className="border p-1 cursor-pointer"
                                value={sub.type}
                                onChange={(e) => updateNestedField(i, j, 'type', e.target.value)}
                              >
                                {SCALAR_TYPES.filter(t => t !== 'object' && t !== 'list_of_dicts')
                                  .map((t) => (
                                    <option key={t} value={t}>
                                      {t}
                                    </option>
                                  ))}
                              </select>
                              <textarea
                                className="border p-1 flex-1 cursor-text"
                                placeholder="Nested description"
                                value={sub.description}
                                onChange={(e) => updateNestedField(i, j, 'description', e.target.value)}
                              />
                              <label className="flex items-center gap-1 cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={sub.required}
                                  onChange={(e) => updateNestedField(i, j, 'required', e.target.checked)}
                                />
                                Required
                              </label>
                              <button
                                className="text-red-500 hover:text-red-700"
                                onClick={() => removeNestedField(i, j)}
                              >
                                ✕
                              </button>
                            </div>
                          ))}
                        <button
                          className="text-sm bg-blue-100 px-2 py-1 rounded mt-1"
                          onClick={() => addNestedField(i)}
                        >
                          + Add nested field
                        </button>
                      </div>
                    )}
                  </div>
                ))}

                <div className="mb-4">
                  <button
                    className="bg-blue-500 text-white px-3 py-1 rounded disabled:opacity-50"
                    onClick={addField}
                    disabled={!lastFieldValid}
                    title={!lastFieldValid ? 'Enter a field name first' : ''}
                  >
                    + Add Field
                  </button>
                </div>

                <div className="mb-4 flex-1 flex flex-col min-h-[200px]">
                  <label className="block font-medium mb-1">Preview JSON Schema</label>
                  <div className="bg-gray-100 p-2 text-sm rounded overflow-auto flex-1 cursor-text">
                    {Object.entries(schema.properties)
                      .filter(([name]) => name.trim() !== '')
                      .map(([name, value], index) => (
                        <div
                          key={name}
                          className="cursor-pointer hover:bg-gray-200 px-2 py-1"
                          onClick={() => setEditIndex(index)}
                        >
                          <code>{name}</code>: <code>{JSON.stringify(value)}</code>
                        </div>
                      ))}
                    <pre className="mt-2 whitespace-pre-wrap break-words">
                      {JSON.stringify(schema, null, 2)}
                    </pre>
                  </div>
                </div>
              </div>

              <div className="mt-4 flex justify-end gap-2">
                <button className="text-gray-600" onClick={onClose}>
                  Cancel
                </button>
                <button
                  className="bg-green-600 text-white px-3 py-1 rounded disabled:opacity-50"
                  onClick={handleSave}
                  disabled={!allFieldsValid}
                  title={!allFieldsValid ? 'Every field must have a name' : ''}
                >
                  Save Schema
                </button>
              </div>
            </>
          ) : (
            <>
              <h2 className="text-lg font-semibold mb-4 handle">Build Pydantic Schema</h2>
              <div className="flex-1 overflow-y-auto">
                <textarea
                  className="w-full h-full border rounded p-2 font-mono"
                  value={pydanticSchema}
                  onChange={(e) => setPydanticSchema(e.target.value)}
                />
              </div>
              <div className="mt-4 flex justify-end gap-2">
                <button className="text-gray-600" onClick={onClose}>
                  Cancel
                </button>
                <button
                  className="bg-green-600 text-white px-3 py-1 rounded disabled:opacity-50"
                  onClick={handleSave}
                  disabled={!pydanticSchema.trim()}
                  title={!pydanticSchema.trim() ? 'Schema cannot be empty' : ''}
                >
                  Save Schema
                </button>
              </div>
            </>
          )}
        </div>
      </Rnd>
    </div>
  );
}
