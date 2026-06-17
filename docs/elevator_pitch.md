# the assistant — Elevator Pitch

the assistant is a local-first personal AI assistant that runs on your own machine. Your data stays with you. the assistant reads your email, watches your calendar, talks to you across UI / Slack / SMS / Telegram, and over time builds a structured memory of your life — not a chat log, but a knowledge graph it uses to make better decisions every day.

This doc captures the features that make the assistant a 5-minute pitch instead of a 5-second one. Everything below is real and shipped unless explicitly marked planned.

---

## Data pods — addressable, privacy-preserving content units

Every piece of content in the assistant — an email, a photo, a transcript, a research result, a note — gets minted as a *pod* with a URI like `datapod:email:50e9d6dc...`. Pods are addressable: agents pass URIs around instead of full content. This is the substrate everything else in the assistant is built on.

The wow moment: **agents can route private content without ever reading it.** When the assistant forwards a confidential email to your spouse, the personal-admin agent only ever sees the pod URI, the recipient, and the action — never the body. The send-email tool fetches and attaches the content at execution time, outside any LLM context. Personal-admin is a courier, not a reader.

This is not hypothetical privacy theater. The agents make routing decisions on pod headers (subject, sender, kind, one-line summary) and let the tool layer handle the actual content unpack. Sensitive material — medical records, legal docs, family conversations — flows through the assistant without entering an LLM prompt.

The same primitive is the planned substrate for federated agent-to-agent transfer. A pod minted on your machine can be addressed by another agent on a partner's machine, with explicit `for_agents` access control.

## Knowledge graph memory

the assistant maintains a structured graph of your life — people, places, projects, beliefs, events — as nodes and edges in SQLite. Not embeddings, not chat history: a real entity-and-relations model with provenance.

Every conversation produces fact proposals; a multi-stage pipeline (extract → canonicalize → resolve → promote → merge) decides which become permanent edges. Nodes have validity dates so "the user lives in Irvine since 2003-09-09" is queryable, not just guessable from chat scrollback.

The KG is what lets the assistant say *"your wife the user's partner"* instead of *"the user who messaged earlier."* It is also what makes the auto-generated wiki, the importance lens, and the /me visualizer possible — they are all KG projections.

## Named agents on a stage

Instead of one monolithic assistant, the assistant delegates to a small team of specialized workers that surface in chat by name:

- **Webby** — web research (search + scrape + reason)
- **Waffle** — general task orchestration
- **Mnemo** — knowledge-graph queries
- **FiloPilo** — email and calendar admin
- **Quimby** — browser automation (Playwright)
- **Hulk** — local shell, read-only
- **Watt** — smart-home control

When you ask "email the user's partner the most recent pod," the chat shows:

> Delegating to Waffle.
> [Waffle] Pulling the newest pod from pod_store.
> Delegating to Mnemo.
> [Mnemo] Searching the knowledge graph for the user's partner's email.
> Delegating to FiloPilo.
> [FiloPilo] Sending the user's partner the pod from the user's account.

You see who is doing what. You can address one by name. They have stable personalities the user remembers and can trust. Each worker has a curated, scoped tool set — Slack-side delegation can never reach a tool the room policy disallows.

## Dayflow orchestrator — autonomous, not just reactive

Most assistants are reactive: you ask, they answer. the assistant has an autonomous workflow engine that processes inbound chat, email, and ticket queues into a curated worklist. A 9-agent pipeline (intake triage → context enrichment → strategic planner → action selector → state mover → relevance cleaner → ...) decides what to act on, in what order, and which items have gone stale.

This is the difference between *"the assistant tells me about my emails when I ask"* and *"the assistant tells me about my emails when there is something I actually need to know."* It runs in the background; you find out about important things in a morning summary or a focused chat ticket, not by scrolling Gmail.

## Local-first, multi-surface

the assistant is Flask + SQLite + ChromaDB + ONNX embeddings. All of your data lives on your machine. The only network calls are to the LLM provider, and only with the slice of context the current task needs.

The same the assistant reaches you on web UI, Slack, SMS (via Twilio), and Telegram. Surface routing is automatic — ask Slack to send the user's partner a pod; the answer comes back in the same Slack thread. A single `OutboundChatPublisher` dispatches every outbound message to the right transport based on the originating room. Adding a new surface (Discord, WhatsApp) is a route handler away.

---

## Other features worth pitching (stubs to expand)

- **Routines** — declarative scheduled tasks (daily insights, nightly KG refresh, weekly wiki rebuild). Five runner types, quiet-hours and per-routine policy.
- **Belief engine** — probabilistic beliefs with bitemporal validity and per-domain confidence decay. *"the user prefers tea over coffee"* has a half-life.
- **Auto-generated wiki** — KG nodes → Markdown pages, one per entity in your life. Bonnie.md, Berkeley.md, the user's partner.md. Refreshed nightly only when bullet text actually changes.
- **/me lens** — concentric-ring visualization of your KG by importance, with depth-cursor zoom that progressively reveals lower-importance nodes.
- **Skill learning (planned)** — the assistant compiles successful task traces into reusable skills with platform-specific knowledge. The same Slack workflow gets faster every time you run it.
- **Pods as A2A protocol (direction)** — the same pod primitive doubles as substrate for federated AI: signed pods, `for_agents` access lists, cross-machine addressing.
