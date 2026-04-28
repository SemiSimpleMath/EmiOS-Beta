import { useState, useRef, useCallback } from 'react';
import * as d3 from 'd3';

export const useGraphPhysics = () => {
  const [physicsConfigured, setPhysicsConfigured] = useState(false);
  const [graphStable, setGraphStable] = useState(false);
  const graphRef = useRef<any>(null);

  const configurePhysics = useCallback(() => {
    if (!graphRef.current || physicsConfigured) return;

    try {
      // d3Force is available on both ForceGraph2D and ForceGraph3D
      graphRef.current.d3Force('link')
        ?.distance(100)
        ?.strength(0.1);

      graphRef.current.d3Force('charge')
        ?.strength(-300)
        ?.distanceMax(400);

      graphRef.current.d3Force('center')
        ?.strength(0.1);

      graphRef.current.d3Force('collision', d3.forceCollide().radius(20));
    } catch (err) {
      // Physics configuration is best-effort
    }

    setPhysicsConfigured(true);
  }, [physicsConfigured]);

  const onEngineStop = useCallback(() => {
    setGraphStable(true);
  }, []);

  const resetPhysics = useCallback(() => {
    setPhysicsConfigured(false);
    setGraphStable(false);
  }, []);

  return {
    physicsConfigured,
    graphStable,
    graphRef,
    configurePhysics,
    onEngineStop,
    resetPhysics,
  };
};
