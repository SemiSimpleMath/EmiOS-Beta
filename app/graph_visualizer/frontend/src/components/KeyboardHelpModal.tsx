import React from 'react';

interface KeyboardHelpModalProps {
  isOpen: boolean;
  onClose: () => void;
}

const KeyboardHelpModal: React.FC<KeyboardHelpModalProps> = ({ isOpen, onClose }) => {
  if (!isOpen) return null;

  const shortcuts = [
    { keys: 'Ctrl/Cmd + R', description: 'Refresh graph data' },
    { keys: 'Ctrl/Cmd + E', description: 'Export graph data' },
    { keys: 'Ctrl/Cmd + H', description: 'Clear highlights' },
    { keys: 'Ctrl/Cmd + A', description: 'Toggle auto-refresh' },
    { keys: 'Ctrl/Cmd + F', description: 'Focus search input' },
    { keys: 'Ctrl/Cmd + Enter', description: 'Execute search' },
    { keys: 'Escape', description: 'Close sidebar/context menu' },
    { keys: 'Shift + ?', description: 'Show this help' },
  ];

  return (
    <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
      <div className="bg-white rounded-lg shadow-xl max-w-md w-full mx-4">
        <div className="p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold text-gray-900">Keyboard Shortcuts</h2>
            <button
              onClick={onClose}
              className="text-gray-400 hover:text-gray-600 focus:outline-none"
            >
              <svg className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
          
          <div className="space-y-3">
            {shortcuts.map((shortcut, index) => (
              <div key={index} className="flex items-center justify-between">
                <span className="text-sm text-gray-600">{shortcut.description}</span>
                <kbd className="px-2 py-1 text-xs font-mono bg-gray-100 border border-gray-300 rounded">
                  {shortcut.keys}
                </kbd>
              </div>
            ))}
          </div>
          
          <div className="mt-6 pt-4 border-t border-gray-200">
            <button
              onClick={onClose}
              className="w-full px-4 py-2 text-sm font-medium text-white bg-blue-600 border border-transparent rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              Got it!
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default KeyboardHelpModal;
