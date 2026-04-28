# EmiOS — Features

## What is EmiOS?

EmiOS is a local-first, open source personal AI assistant that runs entirely on your machine. It's not a chatbot — it's an operating system for your life that proactively manages your day, learns about you over time, and integrates with the tools you already use.

Python-based. Runs on Windows, Mac, and Linux. Excellent on desktop and mobile.

---

## Fully Customizable AI Assistant

- Name your assistant anything — give it a personality, a backstory, a relationship to you
- Choose your LLM provider: OpenAI, Google Gemini, or Anthropic (or mix and match)
- Configure chat style guidelines — how formal, how direct, how much personality
- Every setting is editable through the UI, no config files needed

## Multi-Transport Communication

Talk to your assistant however is convenient:
- **Web UI** — Rich desktop interface with widgets, panels, voice input, image attachments
- **Mobile** — Responsive mobile chat interface
- **Telegram** — Connect as a Telegram bot
- **Slack** — Integrate with Slack channels
- **SMS** — Text your assistant via Twilio

Each channel has its own personality, permissions, and safety rules. Your work Slack bot doesn't act the same as your personal chat.

## Dayflow — Your Autonomous Day Manager

The Dayflow Orchestrator is an event-driven autonomous engine that proactively manages your day. It doesn't wait for you to ask — it watches your schedule, your emails, your calendar, and your context, and acts when it matters:

- **Reminds you intelligently** — Not static "remind me at 3pm" alerts, but because it understands your schedule. It knows you have a meeting in 30 minutes and you're still at home, so it nudges you.
- **Texts you about important emails** — A critical email from your boss lands while you're away? Emi can reach you on Telegram or SMS.
- **Controls your devices** — Dims the lights when it's bedtime, adjusts the thermostat when you leave for work, mutes notifications during meetings.
- **Alerts you about weather** — Rain starting in an hour and you have an outdoor event? You'll know.
- **Manages your calendar from chat** — "Move my 2pm to Thursday" just works. No app switching.
- **Plans and tracks your day** — Creates plans, breaks them into tasks, tracks progress, and follows up.
- **Knows when to leave you alone** — Learns your quiet hours, your focus patterns, and your preferences. Sleeps when there's nothing to do.

As it learns about you, Dayflow gets better and better at knowing what matters, when to act, and when to stay silent.

## It Does All the Usual Stuff — But So Much More

### Email & Calendar
- Gmail sync across multiple accounts with importance filtering
- Send, reply, trash emails from chat
- Google Calendar sync with event creation, editing, search
- Recurring events, reminders, conflict detection

### Tasks & Todos
- Google Tasks integration
- Custom task specs with multi-step execution, conditions, and loops
- Task compilation — write a task once, run it repeatedly
- Pre-built tasks: morning brief, daily summary, email summary, timesheet narrative

### Weather & News
- Automatic weather updates based on your location
- RSS news feed integration
- Both rendered as live widgets in the UI

### Smart Home
- **Nest** thermostat control (temperature, mode, eco)
- **Smart Lights** (Kasa/TP-Link) — on/off, brightness, color, scenes
- **Ring** doorbell camera — video, alerts, motion history

### Web Browsing & Research
- Web search from chat
- URL scraping and summarization
- Full browser automation via Playwright (click, type, fill forms, navigate)

### Music DJ
- Continuous DJ mode that considers everything — time of day, whether you're working or relaxing, your mood, how much sleep you got, what's on your calendar. Morning focus work gets different music than Friday evening wind-down.
- Spotify and Apple Music integration
- Auto-pause when you step away from your computer
- Learns your taste over time from your library and listening patterns

## Learns About You

### Knowledge Graph
EmiOS builds a persistent knowledge graph about your life:
- **Automatic entity extraction** — People, places, concepts mentioned in conversation become nodes
- **Entity cards** — Auto-generated profiles for the people and things in your life
- **Relationship tracking** — Who knows who, how things connect
- **Natural language queries** — "What do I know about Sarah?" pulls from the KG

### Belief Engine
Six domain-specific belief pipelines run nightly:
- **Routine** — Your habits and patterns
- **Health** — Sleep, energy, wellness trends
- **Food** — Dietary preferences and patterns
- **Communication** — How you interact with others
- **Sleep** — Sleep quality and schedule patterns
- **General** — Everything else about you

### Daily & Weekly Insights
- **Daily pipeline** — Archives the day, extracts learnings, builds a timeline, generates an assessment
- **Weekly synthesis** — Cross-day pattern analysis, trend detection, belief candidate generation

## Context-Aware Conversations

The Context Engine activates relevant knowledge when you chat:
- Mentions of a person trigger their entity card
- Current calendar events inform responses
- Your beliefs and preferences shape the assistant's approach
- Recent dayflow activity provides situational awareness

## Growing Ecosystem of AI Agents

Under the hood, EmiOS runs a multi-agent orchestration system. Agents can be generic, specialized, or dynamically take on any role needed:
- Dedicated agents for triage, planning, email, web research, smart home, and more
- Agents produce structured decisions — control nodes and tools execute them
- Managers coordinate agent loops with deterministic state machines
- New agents are easy to add — just a config file, prompt templates, and an output schema

## UI That Tracks Your Day

### Live Widgets
- Calendar — today's events with real-time status
- Weather — current conditions and forecast
- Todos — active task list
- Email — unread count and recent messages
- News — latest RSS items

### Interactive Panels
- Task creation panel — build and edit task specs
- Document panel — Google Docs browser and editor
- GeoGuessr panel — location guessing game with AI hints
- Music player — DJ queue and controls
- Proactive popup — dayflow suggestions with accept/snooze/dismiss

### Developer Tools
- Agent Workbench — test any agent's prompts interactively
- KG Visualizer — explore the knowledge graph visually
- Entity card editor — review and edit entity profiles
- Runtime monitor — see what's running

## Extensible via MCP

Add new capabilities through Model Context Protocol servers:
- Google Maps (geocoding, distance matrix)
- Playwright (browser automation)
- Custom MCP servers you build yourself

Tools are discovered automatically and appear in the agent tool registry.

## Privacy First — Your Data, Your Machine

- **All your data stays on your machine** — SQLite database, knowledge graph, conversation history, beliefs — everything is local
- **LLM calls go to your chosen provider** — OpenAI, Gemini, or Anthropic. Only the current prompt is sent, not your history or personal data
- **Fully extensible to local LLMs** — swap in Ollama or any local model if you want zero cloud dependency
- **No telemetry** — nothing phones home
- **Feature toggles** — disable email, calendar, screenshots, or any feature you don't want
- **Quiet hours** — configure silent windows per feature
- **Git-ignored secrets** — API keys never leave your `.env` file

## Security & Permissions

EmiOS takes a layered approach to keeping agents in check. LLMs make decisions, but they don't get free rein:

- **Room-level access control** — Each room (UI, Telegram, Slack, SMS) defines its own authority level, allowed tools, and visibility rules. Your Telegram bot can't do everything your desktop UI can.
- **Tool permission levels** — Every tool has a risk level and side effects declared in its contract. High-risk tools (sending emails, controlling devices, deleting data) can require explicit user approval before execution. Fully configurable per room and per manager.
- **Scope contracts** — Every manager request carries a scope contract that defines what tools are allowed, what's blocked, and what requires user confirmation for that specific execution context.
- **Per-agent tool allowlists** — Each agent config declares exactly which tools it can access. An email agent can't touch your smart home. A weather agent can't send messages.
- **Approval gates** — Sensitive actions surface as tickets the user can accept, snooze, or dismiss before anything happens.
- **Authority levels** — Rooms have customizable authority levels that control what actions are permitted in each channel.

## The EmiOS System — Build Anything

EmiOS isn't just an assistant — it's a general-purpose multi-agent operating system. The same primitives that power Dayflow, the DJ, and the email manager can be used to build anything:

### Three Layers of Orchestration

- **Orchestrators** — Top-level coordination engines that own long-running autonomous processes. The Dayflow Orchestrator is one; you can build others (a trading monitor, a home automation brain, a content pipeline). Orchestrators are event-driven, self-scheduling, and maintain persistent state across runs.

- **Multi-Agent Managers** — Each manager runs a deterministic agent loop with a state map. A manager owns a team of agents, routes between them, dispatches tools, and returns a result. Managers are composable — one manager can call another through the switchboard. The master room, the email manager, the web research manager, and the KG team are all managers.

- **Agents** — LLM-powered decision units. Each agent has a config (YAML), prompt templates (Jinja2), and a structured output schema (Pydantic). Agents don't execute actions — they produce decisions. Control nodes and tools act on those decisions. This separation means agents are safe, testable, and swappable.

### What You Can Build

The system is designed so that adding a new capability is straightforward:

- **New agent** — Create a directory with `config.yaml`, `prompts/system.j2`, `prompts/user.j2`, and `agent_form.py`. The agent registry discovers it automatically.
- **New tool** — Create a directory with `tool_contract.json` and an `execute()` method. The tool registry discovers it automatically.
- **New manager** — Define a `manager_config.yaml` with agents, control nodes, and a state map. Wire it into an orchestrator or make it callable from the switchboard.
- **New pipeline** — Chain steps together for background data processing (nightly summaries, data ingestion, model training, anything).
- **New room** — Define identity, permissions, and policy for a new communication channel with its own personality and rules.

### Control Nodes — Deterministic Guardrails

Between agents, control nodes handle the non-LLM logic: routing, validation, persistence, state transitions, tool dispatch. They're fast, predictable, and testable. The LLM decides *what* to do; control nodes ensure it happens correctly.

### Blackboard Architecture

Agents within a manager share state through a scoped blackboard — a structured key-value store that carries context, decisions, and results through the agent loop. Blackboards are hierarchical: each manager has its own scope, and a global blackboard enables cross-manager communication.

## Easy Onboarding

- **Setup wizard** — Guided first-run experience walks you through name, timezone, personality, API keys, and integrations. No config files to edit.
- **Multi-provider flexibility** — Only have an OpenAI key? The system automatically maps all agents to your available provider. Add more providers later and agents will use the best model for their tier.
- **Voice input & output** — Speak to your assistant and hear responses. Supports speech-to-text and text-to-speech with configurable voice providers.
- **AFK awareness** — Detects when you're at your desktop and adjusts behavior. Saves resources when you're away, and learns your activity patterns over time to better understand your routine.

## Fun Add-Ons

- **GeoGuessr** — Play location guessing games with AI-powered hints and analysis. Emi looks at the screenshot, gives you clues, and helps you narrow down the answer.
- More games and creative features coming as the community grows.

## Built for Tinkerers

- Python 3.10+ — easy to read, modify, extend
- Flask + SQLite + ChromaDB — simple stack, no Docker required
- Jinja2 prompt templates — edit agent behavior without touching Python
- YAML agent configs — add new agents by creating a directory
- JSON resource files — all state is inspectable and editable
- Comprehensive architecture docs in `docs/architecture/`

---

## Come Join Us

There is so much done, but so much more can be done. EmiOS is an open source project and we'd love your help:

- **Add more tools** — So far we use the Google ecosystem for calendar and email. Come add your favorite platform — Outlook, Notion, Todoist, whatever you use.
- **Expand widgets** — Build new dashboard widgets for fitness tracking, stock portfolios, home energy, or anything you care about.
- **Create alternative UIs** — The current UI is functional but there's room for a React frontend, a terminal UI, an Electron app, or a native mobile app.
- **Create more rooms** — Discord, WhatsApp, Signal, Matrix — any messaging platform can become a room.
- **More smart home controls** — Hue, HomeKit, Home Assistant, Zigbee devices — the tool system makes it straightforward to add new device integrations.
- **Build new orchestrators** — The same system that powers Dayflow can power a fitness coach, a finance tracker, a study planner, or anything that benefits from autonomous AI coordination.

There is literally anything that can be added. The architecture is designed for it.

---

## A Personal Note

I use EmiOS daily because it really works for me and helps me.

I have ADHD, and Emi focuses me and reminds me of all the critical things — and the small things too. It acts as my friend, therapist, and collaborator depending on what I need.

It can be brutally honest. When I was getting bad sleep, it basically told me it was cutting me off from the keyboard and I should just go to bed. When I casually mentioned that I kept waking up in the middle of the night too hot, the next evening around 9 PM it told me that for more comfortable sleep it could cool the house down a bit. I said YES, do that. It had learned that I tend to sleep too hot. From then on, it started automatically cooling the house a bit at night.

I have big plans for Emi. So many ideas for how to build this into a truly useful part of life — not an assistant, but really someone who takes care of me and my family. Improve health, mental health, diet, finances, entertainment. The foundation is here. Come help us build the rest.

— Jukka
