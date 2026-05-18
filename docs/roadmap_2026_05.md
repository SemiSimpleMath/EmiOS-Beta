# EmiOS roadmap — post competitive audit (2026-05-18)

Working roadmap derived from the OpenClaw + Hermes-Agent competitive audit
(see `scratch/competitive_research_2026-05-17.md` for the source data and
the per-case re-marks done with Jukka on 2026-05-18).

This document is the **main todo for the next several days/weeks**. Each
capability is a build target; estimated effort is in engineer-weeks (one
person working at sustained pace).

## Audit headline

After re-marking the 163 cases with corrections (dojo exists,
`daily_insights` exists, belief store is distinct from KG, pods are
broader than secrets):

- **29 ✓ today** (18% of total, 21% of buildable)
- **87 ◐** — mostly missing SKILL.md, not capability
- **47 ✗** — of which **~25 are explicit NOT-build-targets**
- Realistic ceiling with the build-order below: **~73% total / ~86% of buildable**

The single biggest finding: most ✗ marks in the original research weren't
capability gaps. They were missing SKILL.md files for capability stacks
that already exist. HTTP + sandbox + the dojo close most of them.

## The four moats — what NOT to dilute

These are the architectural primitives that compound; every new
capability should be built to deepen them, not work around them:

1. **Belief store** — `belief_engine` pipeline with evidence-weighted
   half-life decay. The "intermittent fasting" demo: multi-month preference
   coherence with clean unlearning when contradicted. Distinct from KG.
2. **Knowledge graph** — entities + relationships + multi-layer
   projections (active KG, NOW-cards, historic wiki), distinct-days
   importance, source-perspective edge ratings.
3. **Pods** — typed reference primitive with privacy class, room scope,
   per-projection authority, mintable from files. Not a vault — a
   capability-gated content layer.
4. **Dojo** — task → trace → trained SKILL.md. Each new capability gets
   amplified into reusable skills automatically.

## Build order (capability-first)

| # | Capability | Effort | Cases unlocked | Notes |
|---|---|---|---|---|
| 1 | **`gh` CLI / GitHub tool** | 1–2 weeks | ~5 dev cases | Smallest unit-of-work, biggest single-week ROI. Add `gh` to bash_manager allowlist OR build a thin `github_manager` wrapping `gh issue/pr/api`. |
| 2 | **STT (Whisper local)** | 1–2 weeks | ~4 cases | Independent capability; closes voice asymmetry. Voice memo ingestion regardless of voice channels. |
| 3 | **HTTP + auth pods (`http_request`)** | 2–3 weeks | ~28 cases | THE meta-unlock. Every future API integration becomes SKILL.md instead of new manager. Pod-aware request/response makes this Emi-uniquely-leveraged. |
| 4 | **Sandboxed code execution (`execute_code`)** | 3–4 weeks | ~15 cases | The other foundation. Docker-backed on Windows-host via WSL2. Allow-listed FS + egress per call. Pairs with HTTP. **"Sandbox is important, people have been asking"** confirmed. |
| 5 | **FS watcher / event triggers** | 1 week | ~3 cases | Small build; completes the autonomous-trigger story (dayflow already handles events). |
| 6 | **5–10 SKILL.md packs on HTTP** | 1 day each | ~10 cases | After #3 ships, each platform integration is a skill: X, Reddit, YouTube transcripts, HN, WHOOP, Sonos, OnStar, Spotify, Meta Ads, MS Graph. Dojo amplifies. |
| 7 | **Doc understanding (PDF/OCR structured)** | 1–2 weeks | ~4 cases | Receipt OCR, deeper arXiv read, legal-contract clauses, structured forms. Combines vision + LLM with Pydantic schema output. |
| 8 | **Image-gen tool** | 3 days | ~3 cases | HTTP-only via fal.ai / Replicate. Ships as a thin SKILL.md after #3. |

**Coverage projection** after this 12-week arc:

| After shipping | Cumulative ✓ | % buildable |
|---|---|---|
| Today | 29 | 21% |
| + gh CLI | 34 | 25% |
| + STT | 38 | 28% |
| + HTTP | 64 | 46% |
| + Sandbox | 79 | 57% |
| + FS watcher | 82 | 59% |
| + 10 SKILL.md packs | 92 | 67% |
| + Doc understanding | 96 | 70% |
| + Image-gen | 99 | 72% |
| + Dojo absorption (6 mo) | ~119 | 86% |

## Pod-aware HTTP — the sketch

`http_request` is the highest-leverage individual build. The shape that
makes it Emi-uniquely-leveraged (not just another LangChain HTTP tool):

```
http_request(
    url: str | pod_id,           # URL itself can be pod-sealed
    method: str,
    headers: dict | pod_id,       # Whole header bundle as a pod (sealed auth)
    body: str | pod_id | bytes,   # Body from a pod (upload a file pod)
    response_pod_kind: optional,  # Stash response as a new pod with declared class
    timeout_s: float,
)
```

The `response_pod_kind` field is the differentiator: API responses can
land in pods with declared privacy class, so a WHOOP biometric response
gets `health.private` and only health-authority skills can read it. No
competitor has this.

## Things NOT to build

Confirmed during the audit walkthrough:

- **Multi-user / family / shared agent** — Emi is single-user by design.
- **Web3 / crypto trading / onchain tools** — out of scope, high-risk niche.
- **Enterprise compliance (EU AI Act, SOC2 audit dashboards)** — not applicable to personal-assistant scope.
- **Kubernetes multi-cluster, AWS/Azure resource control** — Emi runs on Jukka's Windows box, not clusters.
- **Game agents (Minecraft, Mars rover sim)** — novelty.
- **Remote marketplace for skills** — local skill creation via dojo is sufficient.
- **Specialty science domains** — drug discovery, robotics, infosec frameworks.
- **More chat transports (WhatsApp, Discord, Signal, iMessage, Teams)** — explicit deprioritization. Tactical: defer until a use case demands.

## Two decisions worth a separate conversation

1. **`/emi-code` write-enable.** Currently read-only ("discuss-then-ship"
   per memory). Enabling write unlocks ~5 dev cases (fix-test-PR loops,
   autonomous game-dev) but changes the autonomy boundary. Not in this
   roadmap until explicitly decided.
2. **Expose Emi as MCP server (outbound MCP).** Would let Claude Desktop,
   Cursor, etc. use Emi's KG/belief store as a memory backend. Real
   capability gap if wanted; privacy/scope implications are a one-way
   door. Defer until explicit decision.

## Working principle reminder

Per CLAUDE.md and accumulated feedback: build to deepen the moats, not to
match competitor surface area. A new capability that ALSO writes to the
KG, ALSO respects pod authority, ALSO becomes a trainable skill via the
dojo, is worth more than a faster shipping rate.
