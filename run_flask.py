# run_flask.py
import os
import sys
import secrets
from pathlib import Path

# ── Python version guard (must run BEFORE any project import) ──────────────────
# ChromaDB's chromadb_rust_bindings ships wheels for Python 3.10 and 3.11 only.
# On 3.12+ the import fails with an opaque "DLL load failed" and every
# memory/embeddings code path breaks. Catch it here with an actionable message.
if sys.version_info < (3, 10) or sys.version_info >= (3, 12):
    _v = sys.version_info
    print(
        f"\n❌ Python {_v.major}.{_v.minor} is not supported.\n"
        f"   EmiAI needs Python 3.10 or 3.11 (ChromaDB does not ship wheels "
        f"for {_v.major}.{_v.minor}+).\n"
        f"   Install Python 3.11 from python.org and rebuild your venv:\n"
        f"     rmdir /s /q .venv  &&  py -3.11 -m venv .venv\n"
        f"     .venv\\Scripts\\pip install -r requirements_alpha.txt\n",
        file=sys.stderr,
    )
    sys.exit(1)

# Load environment variables from .env file FIRST (before any other imports)
from dotenv import load_dotenv
dotenv_path = Path(__file__).resolve().parent / '.env'
load_dotenv(dotenv_path=dotenv_path)

# ── Auto-generate secrets on first run (MUST happen before create_app) ────────
_env_file = Path(__file__).resolve().parent / '.env'

def _ensure_env_key(key: str) -> str:
    """If env var is missing, generate it and persist to .env."""
    val = os.environ.get(key)
    if val:
        return val
    val = secrets.token_hex(32)
    os.environ[key] = val
    with open(_env_file, 'a', encoding='utf-8') as f:
        f.write(f"{key}={val}\n")
    print(f"Generated {key} and saved to .env")
    return val

_ensure_env_key('FLASK_SECRET_KEY')
_ensure_env_key('JWT_SECRET_KEY')


def _enforce_project_venv() -> None:
    project_root = Path(__file__).resolve().parent
    if sys.platform == "win32":
        expected = project_root / ".venv" / "Scripts" / "python.exe"
    else:
        expected = project_root / ".venv" / "bin" / "python3"
        if not expected.exists():
            expected = project_root / ".venv" / "bin" / "python"

    if not expected.exists():
        raise RuntimeError(
            f"Expected project venv interpreter is missing: {expected}. "
            "Run 'python setup.py' to create .venv before starting Flask."
        )

    actual = Path(sys.executable).resolve()
    expected_resolved = expected.resolve()
    if actual != expected_resolved:
        raise RuntimeError(
            "Refusing to start Flask from non-project interpreter.\n"
            f"Current: {actual}\n"
            f"Required: {expected_resolved}\n"
            "Start with:\n"
            f"  \"{expected_resolved}\" \"{project_root / 'run_flask.py'}\"\n"
            "Or use start.bat (Windows) / start.command (Mac/Linux)."
        )


_enforce_project_venv()

from app.create_app import create_app

# Startup summary (no credentials printed)
_provider = os.environ.get('DEFAULT_LLM_PROVIDER', 'not set')
print(f"EmiAi starting  |  provider={_provider}  |  timezone={os.environ.get('TIMEZONE', 'not set')}")

app, socketio = create_app()
app.secret_key = os.environ['FLASK_SECRET_KEY']


if __name__ == '__main__':
    socketio.run(
        app,
        host='0.0.0.0',
        port=8000,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )
