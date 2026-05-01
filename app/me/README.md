# `app/me/` — the personal lens

Interactive personal-graph navigator. Replaces `/kg-visualizer`. Built clean-room; no imports from `app/graph_visualizer/` or `app/routes/kg_visualizer.py`.

Design doc: `docs/design/me_lens.md`.

## Layout

- `api.py` — Flask blueprint with REST endpoints (`/api/me/...`).
- `live.py` — WebSocket producer for live graph-update broadcasts.
- `pagerank.py` — personalized-PageRank computation and caching.
- `query.py` — chat-input parser (regex templates + LLM fallback).
- `photos.py` — photo URL resolution (entity card → identity → image-pod fallback).
- `frontend/` — Vite + React + TypeScript app. `npm run build` → `frontend/dist/`.

## Allowed dependencies

The lens consumes the KG schema and audited mutation pipeline. It does not consume legacy visualizer code.

OK to import:
- `app.assistant.kg.db.knowledge_graph_db_sqlite` (Node, Edge models).
- `app.models.db_manager` (sessions).
- `app.assistant.entity_management.entity_cards` (EntityCard for one-liners).
- `app.assistant.wiki_generator` outputs (for the wiki side panel — read prose pages).
- `app.assistant.dayflow_orchestrator.state_store` (for "what's happening" if shown).
- `app.assistant.kg.db.kg_maintenance_finding` writers (for the "flag this" button).

Not OK:
- Anything in `app/graph_visualizer/`.
- Anything in `app/routes/kg_visualizer.py`.
- The deprecated `standalone_app.py` pattern.

## Running locally

```
# Backend: started by main Flask app via run_flask.py.
# Frontend dev (hot reload):
cd app/me/frontend
npm install
npm run dev

# Frontend prod (served by Flask at /me):
cd app/me/frontend
npm run build
```

## Status

Pre-v0. See `docs/design/me_lens.md` for the spec and acceptance criteria.
