# EmiOS — Your Personal AI Assistant

EmiOS is a local-first, multi-agent AI assistant that learns about you over time
through a knowledge graph, daily context tracking, and long-term memory.  It runs
entirely on your machine — your data never leaves your computer.

> **Alpha release** — expect rough edges.  Feedback and bug reports are welcome
> via GitHub Issues.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.10+** | Check with `python --version` |
| **Git** | To clone the repository |
| **An LLM API key** | OpenAI, Google Gemini (free tier available), or Anthropic Claude |

Google Calendar, Gmail, and Tasks integration is optional and requires a Google
Cloud OAuth `credentials.json` file.  The setup wizard will guide you through
this if you want it.

---

## Installation

```bash
git clone https://github.com/SemiSimpleMath/EmiOS-Alpha.git
cd EmiOS-Alpha
git checkout master
python setup.py
```

> **Note:** The active development branch is `master`. Make sure you're on it — `main` is outdated.

The setup script handles everything automatically:

1. Verifies Python version
2. Creates a `.venv` virtual environment
3. Installs all dependencies (including Knowledge Graph support via ONNX — no
   PyTorch required)
4. Initialises the SQLite database
5. Seeds the KG taxonomy
6. Creates a desktop shortcut (`EmiOS`)

When setup completes, launch EmiOS using the desktop shortcut or from the
terminal:

```bash
# Windows
.\emi.bat

# macOS / Linux
./emi.command
```

On first launch the **setup wizard** opens in your browser at
`http://localhost:8000/setup` and walks you through:

- Your name, pronouns, and timezone
- Choosing an LLM provider and entering your API key
- Naming and personalising your assistant
- (Optional) Adding important people for the knowledge graph

After the wizard finishes you're dropped into the chat UI.  Subsequent launches
skip the wizard and go straight to chat.

---

## Running EmiOS

**Desktop shortcut (recommended):** Double-click the `EmiOS` shortcut on your
Desktop.  If the server is already running it simply opens the browser.

**Terminal:**

```bash
# Windows
.\emi.bat

# macOS / Linux
./emi.command

# Or directly:
python start.py
```

The app runs at **http://localhost:8000**.

---

## What's Included

| Feature | Description |
|---|---|
| **Multi-agent chat** | Conversation routed through specialised agents (triage, planner, one-shot, etc.) |
| **Knowledge Graph** | Automatically extracts entities and relationships from your conversations |
| **Entity Cards** | Auto-generated profiles for people, places, and concepts you discuss |
| **Daily Context** | Dayflow pipeline tracks your schedule, status, and daily themes |
| **Expected Calendar** | Enriched view of today's schedule with status tracking |
| **Background Data Fetch** | Periodic email, calendar, weather, news, and task updates |
| **KG Visualiser** | Interactive graph viewer (Dev menu) |
| **Google Integration** | Gmail, Calendar, and Tasks (requires OAuth setup) |
| **Telegram Bot** | Optional — connect via Telegram |
| **Music / DJ** | Spotify and Apple Music integration (optional) |

---

## Configuration

All configuration lives in a `.env` file at the project root (created by the
setup wizard).  See `.env.example` for the full list of available variables.

Agent-level LLM routing is configured per-agent in each agent's `config.yaml`.
The `DEFAULT_LLM_PROVIDER` in `.env` is only a fallback.

User preferences and feature toggles are managed through the **Settings** page
in the UI (gear icon).

---

## Project Structure (Key Directories)

```
EmiOS-Alpha/
├── setup.py                  # First-time setup script
├── start.py                  # Launcher (auto-detects venv)
├── run_flask.py              # Flask application entry point
├── emi.bat / emi.command     # OS-specific launcher with toggle logic
├── requirements_alpha.txt    # Python dependencies
├── .env.example              # Environment variable reference
├── app/
│   ├── templates/            # HTML templates (chat UI, settings, etc.)
│   ├── static/               # CSS, JS, images
│   ├── routes/               # Flask route handlers
│   ├── models/               # SQLAlchemy models
│   ├── assistant/
│   │   ├── agents/           # Agent definitions (config.yaml + prompts)
│   │   ├── pipelines/        # Data pipelines (dayflow, KG, entity cards)
│   │   ├── kg/               # Knowledge graph core
│   │   ├── background_task_manager/
│   │   └── routine_manager/  # Scheduled routine execution
│   └── graph_visualizer/     # KG visualiser (React frontend, pre-built)
├── resources/                # User data, assistant config, context files
├── configs/                  # Routine definitions, tool configs
└── day_context/              # Daily pipeline outputs (auto-generated)
```

---

## Troubleshooting

**"provider=not set" at startup** — Add `DEFAULT_LLM_PROVIDER=openai` (or
`gemini` / `anthropic`) to your `.env` file.  This is cosmetic; agents use their
own `config.yaml` for LLM routing.

**Setup wizard keeps appearing** — Make sure `SETUP_COMPLETE=true` is in your
`.env` file.

**KG tables missing / merge errors** — Delete `emi.db` and restart.  Tables are
recreated automatically on boot.

**Calendar widget empty** — Calendar data requires either Google OAuth
(Calendar integration) or an active dayflow pipeline run.  Check Settings to
ensure the calendar feature is enabled.

**Port 8000 already in use** — Another instance may be running.  The launcher
scripts detect this and open the browser instead of starting a second server.

---

## License

This project is released under the [MIT License](LICENSE).
