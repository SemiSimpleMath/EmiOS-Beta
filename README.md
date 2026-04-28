# EmiOS

A local-first personal AI assistant. Runs on your laptop, builds a knowledge graph of things you tell it, and writes a personal wiki of your life over time.

This README covers install only. For the first-week walkthrough, troubleshooting, and how it works day-to-day, see [`BETA.md`](BETA.md).

## Prerequisites

- **Python 3.10 or newer.** Check with `python --version` (or `python3 --version` on Linux/Mac).
- **About 1 GB of disk space** (mostly the `.venv` and a small embedding-model cache).
- **One LLM API key** — at least one of:
  - `OPENAI_API_KEY` — easiest, most tested
  - `ANTHROPIC_API_KEY`
  - `GOOGLE_API_KEY` (for Gemini)
- **15 minutes** for first install.

Optional integrations (Google Calendar/Gmail, Twilio SMS, Slack, Telegram, OpenWeather) are configured later from the in-app Settings page.

## Install

### Windows

1. Install Python 3.10+ from [python.org](https://www.python.org/downloads/). Tick **"Add Python to PATH"** during install.
2. If pip complains about Visual C++ build tools, install **Microsoft C++ Build Tools** from Microsoft's site (free), restart, retry.
3. Clone and set up:
   ```cmd
   git clone https://github.com/SemiSimpleMath/EmiOS-Beta.git
   cd EmiOS-Beta
   python setup.py
   ```
4. Launch:
   ```cmd
   .\emi.bat
   ```
   Or double-click `emi.bat` from Explorer. Browser opens to `http://localhost:8000` automatically.

### macOS

1. Python 3.10+ usually ships with macOS. If you need a newer version, install via [python.org](https://www.python.org/downloads/) or `brew install python@3.12`.
2. On Apple Silicon (M1/M2/M3), if pip complains about Xcode tools:
   ```bash
   xcode-select --install
   ```
3. Clone and set up:
   ```bash
   git clone https://github.com/SemiSimpleMath/EmiOS-Beta.git
   cd EmiOS-Beta
   python3 setup.py
   ```
4. Launch:
   ```bash
   ./emi.command
   ```
   Or double-click `emi.command` from Finder.

### Linux (Ubuntu / Debian)

1. Install Python and the system libraries the dependencies need:
   ```bash
   sudo apt update
   sudo apt install python3-venv python3-pip lsof \
                    build-essential libssl-dev libffi-dev \
                    libxml2-dev libxslt1-dev
   ```
   `python3-venv` is not bundled by default on Ubuntu — without it `python3 -m venv` fails silently.
2. Clone and set up:
   ```bash
   git clone https://github.com/SemiSimpleMath/EmiOS-Beta.git
   cd EmiOS-Beta
   python3 setup.py
   ```
3. Launch:
   ```bash
   chmod +x emi.command   # one-time, git doesn't preserve the bit
   ./emi.command
   ```
   Or run `python3 start.py` directly. Browser opens to `http://localhost:8000`.

### Windows Subsystem for Linux (WSL)

Install Emi inside a WSL Ubuntu distro. Two notes specific to WSL:

- **Use Ubuntu, not Alpine.** Several heavy dependencies (chromadb, onnxruntime, numpy, lxml) ship glibc-only wheels on PyPI. On Alpine's musl libc they fall back to source builds and onnxruntime in particular doesn't compile cleanly. Save yourself the headache.
- **Keep the project on the Linux filesystem.** Clone into `~/EmiOS-Beta`, not `/mnt/c/...` or `/mnt/e/...`. Running Python out of `/mnt/...` is 5–10× slower because of the WSL↔NTFS bridge, and SQLite locking misbehaves.

Steps:

1. From Windows PowerShell:
   ```powershell
   wsl --install -d Ubuntu
   ```
   First launch will prompt for a username and password.

2. Inside the Ubuntu shell, install system dependencies (same as Linux above):
   ```bash
   sudo apt update
   sudo apt install python3-venv python3-pip lsof \
                    build-essential libssl-dev libffi-dev \
                    libxml2-dev libxslt1-dev
   ```

3. Clone and set up:
   ```bash
   cd ~
   git clone https://github.com/SemiSimpleMath/EmiOS-Beta.git
   cd ~/EmiOS-Beta
   python3 setup.py
   ```

4. Launch:
   ```bash
   chmod +x emi.command
   ./emi.command
   ```
   `xdg-open` will fail because WSL has no GUI — that's expected. Open `http://localhost:8000` in your **Windows** browser. WSL2 forwards localhost to Windows automatically.

## After install

On first launch you'll land on a setup wizard that walks you through your profile, your assistant's name and personality, and your API keys. About 10 minutes.

For what to try in your first week, the in-app menus, scheduled jobs, settings, resetting, and how to file feedback, read [`BETA.md`](BETA.md).

## Resetting

If something gets corrupted or you want to start over:

1. Stop Emi (close the terminal window or use the menu's Shut Down option).
2. Delete `emi.db`, the `chroma_db/` folder, and the `resources/` folder.
3. Re-run the launcher. The setup wizard will re-trigger.

Your `.venv` and installed packages stay; only your data gets wiped.

## License

MIT. See [`LICENSE`](LICENSE).
