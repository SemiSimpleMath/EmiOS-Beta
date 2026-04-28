import { useState, useEffect, useCallback } from 'react';

export const useAutoRefresh = (refetch: () => void) => {
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [refreshInterval, setRefreshInterval] = useState(30); // seconds

  const toggleAutoRefresh = useCallback(() => {
    setAutoRefresh(prev => !prev);
  }, []);

  const updateRefreshInterval = useCallback((interval: number) => {
    setRefreshInterval(interval);
  }, []);

  // Auto-refresh effect
  useEffect(() => {
    if (autoRefresh && refreshInterval > 0) {
      const interval = setInterval(() => {
        refetch();
      }, refreshInterval * 1000);
      return () => clearInterval(interval);
    }
  }, [autoRefresh, refreshInterval, refetch]);

  return {
    autoRefresh,
    refreshInterval,
    toggleAutoRefresh,
    updateRefreshInterval
  };
};
