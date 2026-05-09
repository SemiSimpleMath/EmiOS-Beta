# Welcome to the Emi beta

Thanks for trying this. Emi is a personal AI assistant that runs on **your** laptop, builds a knowledge graph of things you tell it, and writes a personal wiki of your life over time. It's early. It will have rough edges. If something breaks, ping me — that's literally why you're here.

This page is the only one you need to read.

---

## Before you start

You need:

- [ ] **Python 3.10 or 3.11.** Python 3.12+ is not supported — ChromaDB does not ship wheels for it. (Run `python --version` to check. Mac: usually built-in but often older; `brew install python@3.11` works. Windows: install Python 3.11 from python.org and tick "Add to PATH".)
- [ ] **About 1 GB of disk space.** Mostly the `.venv` (~700 MB) plus a small embedding-model cache and the (initially tiny) databases that grow as you use it.
- [ ] **An API key** for at least one of these:
  - OpenAI (`OPENAI_API_KEY`) — easiest, most tested
  - Anthropic / Claude (`ANTHROPIC_API_KEY`)
  - Google Gemini (`GOOGLE_API_KEY`)
- [ ] **15 minutes** for the first install. The setup script does most of it.

You **do not** need:
- Google Calendar / Gmail integration (optional, can add later)
- Twilio / SMS (optional)
- Slack / Telegram (optional)

If you want any of those later, the in-app Settings page walks you through it.

---

## First-time install (one-time)

Open a terminal in the project folder.

### 1. Run the setup script

```bash
python setup.py
```

This will:
1. Check your Python version.
2. Create a `.venv/` folder for Emi's dependencies.
3. Install ~90 Python packages. **This is the slowest step — give it 5–10 minutes.**
4. Initialize Emi's database files.
5. Seed the taxonomy ontology.
6. Create a desktop shortcut.

Expect a wall of output. Lines starting with ✅ are good. Lines starting with ⚠️  are warnings (usually safe to ignore — credentials.json missing means "Google integration not set up," that's fine for now). Lines starting with ❌ are real errors — ping me with the line.

When it's done you'll see a "SETUP COMPLETE!" banner.

### 2. Launch Emi

From the project folder:

- **Windows:** double-click `emi.bat` (or run `.\emi.bat` from the terminal)
- **Mac/Linux:** double-click `emi.command` (or run `./emi.command` from the terminal)
- **Either:** `python start.py`

A terminal window will open showing Emi's startup logs. After ~10 seconds, your browser should automatically open to **http://localhost:8000**.

If the browser doesn't open, just go to that URL manually.

### 3. Walk the setup wizard

First time you visit, you'll see a wizard with 8 steps. It asks about you, important people in your life, work, your bio, what you want Emi to be called, conversation style, and finally your API keys. Skip anything you don't want to fill in (almost everything has a sensible default).

The wizard takes another ~10 minutes. **What you put in here seeds Emi's knowledge graph** — Emi will know about whoever you mention, in the way you describe them. There's no cloud upload; everything stays on your machine.

Click "Done" on the last step. You'll land on the chat page.

### 4. Say hi

Type something into the chat. "Hi, can you tell me what you know about me?" is a good first message. Emi will respond with what it picked up from the wizard.

**That's session 1 done.** Close the tab if you want, or keep playing. Either way, the install is finished.

---

## Day 2 and onward — how to start Emi again

Two clicks:

1. Double-click the desktop shortcut (or run `emi.bat` / `emi.command` / `python start.py`).
2. The terminal window opens; the browser opens to localhost:8000 a few seconds later.

That's it. You don't run `setup.py` again — only the first time.

If the terminal window from yesterday is still open and Emi is still running, you can just open the browser to **http://localhost:8000** directly. The launcher script will detect Emi is already running and just open the browser tab.

---

## Three things to try in your first week

Each of these shows off something the architecture does that other chatbots don't.

### Day 1 — "Tell Emi about yesterday"

Have a 5-minute conversation about something real that happened — a meeting, a meal, a workout, a conversation with someone. Don't censor for "what an AI would care about." Just talk. Emi reads it, extracts what's worth remembering, and stores it overnight.

### Day 2 — "Ask Emi what it remembers"

Open Emi the next day and ask: *"What do you remember from yesterday?"*

This is the moment that's different from cloud chatbots. Emi has a real knowledge graph. It should pull back what you told it, not a transient context window.

### Day 3 — "Look at the wiki Emi built about you"

Click the **My Life** menu (book-icon, top-right). Open **Wiki**. Emi has been quietly generating a personal wiki of you and the people / places / things you've mentioned. Browse it. Click around. It's yours.

Then click **Entity Cards** in the same menu — short structured profiles for each person/place/thing. Same data, different shape.

---

## What's running in the background

Emi has scheduled jobs that fire while you're not interacting:

- **Every minute:** internal heartbeat work (cheap, no LLM calls).
- **Every few minutes:** location refresh, situation audit, weather/email/calendar/news fetch (only if you configured those).
- **Once a night (between midnight and 1am local):** the heavy stuff — daily insights, belief engine, knowledge graph maintenance, wiki refresh, morning briefing prep.

You can see all of these at **http://localhost:8000/routines** (or via the menu under "Routines & Schedule"). Click any of them to see when it last ran, what it produced, and run it on demand.

If your laptop sleeps overnight, the night-time jobs catch up next time you open Emi. Nothing crashes; they just queue.

---

## Stopping Emi

Two ways:

1. **Close the terminal window** that the launcher opened. Emi shuts down cleanly.
2. From the chat menu (☰ on the top-left): **Shut Down EmiOS**.

If you want Emi running in the background while you do other things, just leave the terminal open and switch tabs.

---

## When something goes wrong — ping me

These are the kinds of things to send me:

- **Setup script failed.** Send me the last ~30 lines of the terminal output.
- **Browser shows a Python error page.** Take a screenshot, send it.
- **Emi said something weird / wrong / off.** Tell me what you asked and what it said. The text alone is enough; I can look up what happened in the logs on your machine.
- **A scheduled job didn't run.** Open `/routines`, find the one in question, screenshot the row.
- **Emi forgot something it should have remembered.** This is the most useful kind of bug — tell me what you said, what you expected it to remember, and what it said when you asked.
- **The chat is slow.** Tell me what you asked. Some queries are genuinely slow (multi-step research); others are slow because something is broken.

If you want to send me everything Emi knows about its own state for debugging, run this from the project folder:

```bash
python emi_diag.py > diag.txt
```

(*If that command doesn't exist yet, just send me whatever's in the most recent file under `logs/`.*)

**The fastest way to reach me:** [<your-preferred-channel>]

---

## What's likely to go wrong (forewarned is forearmed)

- **The first install of `chromadb` can take a couple of minutes.** It's a vector database. The first time it runs, it downloads a small embedding model (~30 MB). Expect the first launch to be slower than later ones.
- **On a Mac with Apple Silicon (M1/M2/M3):** if pip complains about any package needing Xcode tools, run `xcode-select --install` once and try again.
- **On Windows:** if you get errors about Visual C++ build tools, install **"Microsoft C++ Build Tools"** from Microsoft's site (free), restart, retry.
- **Port 8000 already in use:** something else on your machine grabbed it. Kill that, or restart your laptop.
- **Setup wizard freezes mid-step:** refresh the page. Drafts auto-save; you won't lose progress.

---

## Things this isn't yet

So you don't expect them and get disappointed:

- **Not a voice assistant.** It can speak (toggleable Speak Mode) but the primary interface is chat.
- **Not connected to your phone unless you set up Twilio / Telegram / Slack.** The web UI is the default.
- **Not a search engine.** It has the `search_web` tool but uses it sparingly.
- **Not aware of "right now" without you telling it.** Calendar and email integrations exist but require their own setup.
- **Not a finished product.** This is alpha. The architecture is real and tested; the polish isn't.

---

## What you can change

Almost everything is editable from inside the app:

- **Settings → About You / Your Bio** — edit anything you put in the wizard.
- **My Life → Entity Cards** — edit the cards Emi generated about people / places.
- **My Life → Wiki** — read what Emi wrote about you. Edits sync back next refresh.
- **Routines & Schedule** — turn things on or off, change when they run.
- **Settings → Features** — toggle integrations on/off.
- **Settings → Quiet Mode** — tell Emi when not to fire scheduled jobs.

Don't be precious. Click things. The data is local; the worst case is you reset and start over.

---

## Resetting

If something gets corrupted or you want to start fresh:

1. Close Emi (terminal close, or menu → Shut Down).
2. Delete the `emi.db` file in the project root.
3. Delete the `chroma_db/` folder.
4. Delete `resources/`.
5. Re-run `emi.bat` / `emi.command`. The setup wizard will re-trigger.

Your `.venv` and installed packages stay; only your data gets wiped.

---

## That's it

Have fun. If you forget anything in this doc, it's all browseable from the in-app menus. If you get stuck, ping me — the worst-case time-to-recovery is "I'll fix it on my end and push an update; you `git pull` and restart."

— *the maintainer*
