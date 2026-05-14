import { useState, useRef, useCallback } from 'react';
import * as d3 from 'd3';

// Reveal the canvas once the d3 force engine has done this many ticks since
// the last reset. For ~6k-node graphs this gives the layout time to spread
// without relying on onEngineStop, which doesn't always fire reliably on
// large graphs (the engine can hover near alpha=alphaMin without stopping).
const REVEAL_AFTER_TICKS = 300;

export const useGraphPhysics = () => {
  const [physicsConfigured, setPhysicsConfigured] = useState(false);
  const [graphStable, setGraphStable] = useState(false);
  // Surface the tick count to the UI so the user can SEE that physics is
  // actually running (vs. a frozen overlay). Updated by onEngineTick.
  const [tickCount, setTickCount] = useState(0);
  const graphRef = useRef<any>(null);
  const tickCountRef = useRef(0);

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

  // Two signals can reveal the canvas:
  //  (a) onEngineStop — engine settled cleanly (small graphs)
  //  (b) onEngineTick — N ticks have run (large graphs, where alpha never
  //      quite reaches alphaMin and onEngineStop is silent)
  const onEngineStop = useCallback(() => {
    setGraphStable(true);
  }, []);

  const onEngineTick = useCallback(() => {
    tickCountRef.current += 1;
    // Update the UI counter every 10 ticks to avoid render thrash.
    if (tickCountRef.current % 10 === 0) {
      setTickCount(tickCountRef.current);
    }
    if (tickCountRef.current === REVEAL_AFTER_TICKS) {
      setGraphStable(true);
    }
  }, []);

  const resetPhysics = useCallback(() => {
    setPhysicsConfigured(false);
    setGraphStable(false);
    tickCountRef.current = 0;
    setTickCount(0);
  }, []);

  return {
    physicsConfigured,
    graphStable,
    tickCount,
    graphRef,
    configurePhysics,
    onEngineStop,
    onEngineTick,
    resetPhysics,
  };
};
