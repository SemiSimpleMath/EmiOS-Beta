> **⚠️ ARCHIVED / SPECULATIVE — NOT WIRED (2026-06-17).** A design spec; the referenced runner (`tmp_run_kg_cold_start_pipeline.py`) was deleted and the end-to-end pipeline is not currently runnable (`kg_cold_start_orchestrator/` holds only a config). Model ids are dated. Kept as a design artifact.

---

# KG Cold-Start Investigation Pipeline — Spec

## Goal

When a user mentions a person or event (e.g. "Sam is coming to visit"), the system
should spring into action and build a rich, grounded briefing — without the user having
to ask for it.

---

## End State (two-phase)

### Phase 1 — Factual ✅ COMPLETE

Extract everything the graph actually knows about the intersection of the two people.

Outputs:
- Who is the visitor and what is their relationship to the user
- Shared history, shared connections, shared concepts
- Pending goals or actions already in the graph (letters to write, visits planned)
- Structurally implied but unrecorded edges (two people sharing an advisor with no direct edge)
- Specific unknowns that are answerable via a targeted KG query (visit status, letter status)
- Questions to surface to the user where the graph has a genuine gap

**Implemented as a two-step pipeline (no orchestrator needed):**

```
Step 1 (~8s):  multi_seed_activation → compact convergence briefing
Step 2 (~13s): single Gemini 3 Flash call → structured dossier (markdown)
Step 3 (~2min): single emi_team_manager → classify + research + surface questions
─────────────────────────────────────────────────────────
Total: ~2.5 minutes end-to-end
```

Script: `app/assistant/test/tmp_run_kg_cold_start_pipeline.py`

### Phase 2 — Inference (next)

Use the factual dossier to generate what is *likely but unrecorded*, leveraging the LLM's
ability to reason about people from their intersection profile.

Outputs:
- Confident inferences about shared interests, tastes, conversational style
  (flagged clearly as inferred, not stated as graph facts)
- Visit ideas: activities, food, places, conversation topics
- Emotionally resonant angles (e.g. shared loss of a mentor)
- Actionable suggestions ranked by confidence and effort

**Architecture for Phase 2:**
The dossier from Phase 1 becomes the input. An orchestrator with parallel children is
well-suited here — each child speculates on a different dimension (activities, food,
academic topics, emotional angles) independently, and a synthesis child combines them.
This is the natural home for the `kg_cold_start_orchestrator` work already built.

---

## Phase 1 Pipeline — Architecture

```
User message: "Sam is coming to visit"
        ↓
[Step 1] multi_seed_activation(Sam, Alex)        ~8s
         → convergence context: ranked nodes, second-wave expansions, descriptions
        ↓
[Step 2] Single Gemini 3 Flash call (no agent, no schema)   ~13s
         Input:  convergence briefing (~3-4k tokens)
         Output: structured dossier (markdown) with sections:
           - Relationship
           - Shared History & Academic World
           - Pending Goals & Actions
           - Missing Edges (structurally implied, not recorded)
           - Questions for the user
           - Researchable via KG (follow-up questions for the agent)
        ↓
[Step 3] Single emi_team_manager (ask_kg + ask_user)         ~2min
         Input:  dossier from Step 2
         - Generates visit questions and concerns
         - Classifies: KG-researchable vs needs user input
         - Calls ask_kg (max 3 targeted calls)
         - Returns:
             answered{}       — questions resolved from the KG
             open_for_user[]  — questions Alex needs to answer personally
             suggested_actions[] — concrete next steps
```

---

## Phase 2 Pipeline — Architecture (planned)

```
[Input]  Phase 1 dossier (structured markdown)
        ↓
[Orchestrator] kg_cold_start_orchestrator (already built)
         Wave 1 — parallel inference children (each one angle):
           A. Activities: what would Alex and Sam enjoy doing together?
           B. Food / restaurants: infer from known preferences and context
           C. Conversation topics: academic, personal, shared loss of Varadarajan
           D. Practical logistics: travel, accommodation, duration questions
         Wave 2 — synthesis child: combine all into a visit brief
        ↓
[Output] Full visit brief:
         - factual context (from Phase 1)
         - inferred suggestions (clearly flagged)
         - questions for user
         - suggested actions
```

The orchestrator's parallel architecture is well-matched to Phase 2 because:
- The inference tasks are genuinely independent (food ≠ activities ≠ topics)
- Each child can be speculative without needing KG access
- A synthesis child combining 4 independent angle reports is exactly what the
  curator/architect loop is designed for

---

## ask_kg Performance

After tuning (March 2026):
- `k_edges`: 40 → 20  (fewer evidence edges per LLM call)
- `max_hops`: 3 → 2   (smaller BFS neighborhood)
- Model: `gpt-5-mini` → `gemini-3-flash-preview`
- Result: 60-90s per call → **6-14s per call**

Orchestrator brain agents (curator, router, architect) also switched to
`gemini-3-flash-preview`: 15-30s per firing → **5-7s per firing**.

---

## Current Status

- [x] multi_seed_activation works — Varadarajan surfaces correctly, David Weisbart in wave 2
- [x] Convergence context formatted and passed correctly
- [x] Phase 1 pipeline complete and tested end-to-end (~2.5 min, clean output)
      — dossier quality: "mathematical sibling" framing, Varadarajan death angle, Dave missing edge
      — ask_kg tuned to 6-14s per call (was 60-90s)
      — all orchestrator brain agents on Gemini 3 Flash
- [x] Orchestrator pipeline built and functional (kg_cold_start_orchestrator)
      — preserved for Phase 2 use, not needed for Phase 1
      — runtime ~6min with Gemini children
      — known issue: router occasionally cancels active children mid-flight
- [ ] Phase 1: wire as background trigger from ingress (person mention + upcoming event)
- [ ] Phase 1: store dossier in KG/resource so subsequent conversations have it warm
- [ ] Phase 1: surface open_for_user questions in the conversation UI
- [ ] Phase 2: inference layer (orchestrator with speculative parallel children)

## Known data quality issues

- Goal nodes with no completion status get scored as active — e.g. "Share five-year plan
  with Sam" dominated the child's output even though the plan may be done/abandoned.
  Fix: add a completed_at or status field to Goal nodes so the algorithm can down-score
  stale goals. For now the system correctly surfaces these as questions to ask the user
  ("what happened with X?") which is the right fallback — user clarifies, KG gets updated.

- Sam's current institution and role are not recorded in the KG. Surfaced correctly as
  a missing edge in the dossier. Worth recording after user confirms.
