import { useEffect, useCallback } from 'react';

interface KeyboardShortcutsProps {
  onRefresh?: () => void;
  onExport?: () => void;
  onClearHighlights?: () => void;
  onToggleAutoRefresh?: () => void;
  onSearch?: () => void;
  onCloseSidebar?: () => void;
}

export const useKeyboardShortcuts = ({
  onRefresh,
  onExport,
  onClearHighlights,
  onToggleAutoRefresh,
  onSearch,
  onCloseSidebar
}: KeyboardShortcutsProps) => {
  
  const handleKeyDown = useCallback((event: KeyboardEvent) => {
    // Don't trigger shortcuts when typing in input fields
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) {
      return;
    }

    const { key, ctrlKey, metaKey, shiftKey } = event;
    const isCtrlOrCmd = ctrlKey || metaKey;

    switch (key) {
      case 'r':
        if (isCtrlOrCmd) {
          event.preventDefault();
          onRefresh?.();
        }
        break;
      case 'e':
        if (isCtrlOrCmd) {
          event.preventDefault();
          onExport?.();
        }
        break;
      case 'h':
        if (isCtrlOrCmd) {
          event.preventDefault();
          onClearHighlights?.();
        }
        break;
      case 'a':
        if (isCtrlOrCmd) {
          event.preventDefault();
          onToggleAutoRefresh?.();
        }
        break;
      case 'f':
        if (isCtrlOrCmd) {
          event.preventDefault();
          // Focus search input (this would need to be implemented in the component)
          const searchInput = document.querySelector('input[type="text"]') as HTMLInputElement;
          searchInput?.focus();
        }
        break;
      case 'Enter':
        if (isCtrlOrCmd) {
          event.preventDefault();
          onSearch?.();
        }
        break;
      case 'Escape':
        event.preventDefault();
        onCloseSidebar?.();
        break;
      case '?':
        if (shiftKey) {
          event.preventDefault();
          // Toggle help modal (this would need to be implemented in the component)
          console.log('Show keyboard shortcuts help');
        }
        break;
    }
  }, [onRefresh, onExport, onClearHighlights, onToggleAutoRefresh, onSearch, onCloseSidebar]);

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);
};
