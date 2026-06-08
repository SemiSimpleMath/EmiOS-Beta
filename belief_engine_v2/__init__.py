"""belief_engine_v2 — a from-scratch rebuild of the belief store.

Design: scratch/BELIEF-ENGINE-NEW.md. Build order: #1 event sourcing (this commit),
#2 canonical identity, #3 contextual retrieval, #4 justification-derived status,
#5 cadence-aware decay.

Self-contained: pure-Python + sqlite3 stdlib, its own DB file, no app/DI bootstrap,
never touches emi.db. The live engine keeps running until v2 cutover.
"""
__version__ = "0.1.0-event-sourcing"
