import { useState, useEffect, useCallback } from 'react';
import { ContextMenuState } from '../types/graph';

export const useContextMenu = () => {
  const [contextMenu, setContextMenu] = useState<ContextMenuState>({
    isOpen: false,
    x: 0,
    y: 0,
    node: null
  });

  const openContextMenu = useCallback((x: number, y: number, node: { id: string; label: string } | null) => {
    setContextMenu({
      isOpen: true,
      x,
      y,
      node
    });
  }, []);

  const closeContextMenu = useCallback(() => {
    setContextMenu({
      isOpen: false,
      x: 0,
      y: 0,
      node: null
    });
  }, []);

  // Add global click listener for context menu
  useEffect(() => {
    if (contextMenu.isOpen) {
      const handleClick = (event: MouseEvent) => {
        closeContextMenu();
      };
      
      document.addEventListener('click', handleClick);
      return () => document.removeEventListener('click', handleClick);
    }
  }, [contextMenu.isOpen, closeContextMenu]);

  return {
    contextMenu,
    openContextMenu,
    closeContextMenu
  };
};
