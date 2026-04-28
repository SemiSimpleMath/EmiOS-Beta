"""
start.py — Launch EmiAI without needing to activate the virtual environment.

Usage:
    python start.py

This script auto-detects the .venv Python and runs run_flask.py through it.
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def get_venv_python():
    if sys.platform == "win32":
        candidates = [ROOT / ".venv" / "Scripts" / "python.exe"]
    else:
        candidates = [
            ROOT / ".venv" / "bin" / "python3",
            ROOT / ".venv" / "bin" / "python",
        ]
    for p in candidates:
        if p.exists():
            return str(p)
    return None

def main():
    venv_python = get_venv_python()
    if not venv_python:
        print("ERROR: Virtual environment not found.")
        print("Please run the setup first:  python setup.py")
        sys.exit(1)

    run_flask = ROOT / "run_flask.py"
    if not run_flask.exists():
        print(f"ERROR: run_flask.py not found at {run_flask}")
        sys.exit(1)

    print(f"Starting EmiAI...")
    print(f"Open http://localhost:8000 in your browser.\n")
    os.chdir(ROOT)
    result = subprocess.run([venv_python, str(run_flask)])
    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
