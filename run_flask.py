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
        f"     .venv\\Scripts\\pip install -r requirements.txt\n",
        file=sys.stderr,
    )
    sys.exit(1)

# Load environment variables from .env FIRST (before any other imports). The .env
# lives under the data dir (EMI_DATA_DIR) in the packaged app; in dev it defaults to
# the repo root. Resolved inline (mirrors path_utils.get_env_file) because this runs
# before any app import.
from dotenv import load_dotenv
if os.environ.get("EMI_ENV_FILE"):
    _env_file = Path(os.environ["EMI_ENV_FILE"]).expanduser()
elif os.environ.get("EMI_DATA_DIR"):
    _env_file = Path(os.environ["EMI_DATA_DIR"]).expanduser() / ".env"
else:
    _env_file = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_env_file)

# ── Auto-generate secrets on first run (MUST happen before create_app) ────────

def _ensure_env_key(key: str) -> str:
    """If env var is missing, generate it and persist to .env (under the data dir)."""
    val = os.environ.get(key)
    if val:
        return val
    val = secrets.token_hex(32)
    os.environ[key] = val
    _env_file.parent.mkdir(parents=True, exist_ok=True)
    with open(_env_file, 'a', encoding='utf-8') as f:
        f.write(f"{key}={val}\n")
    print(f"Generated {key} and saved to {_env_file}")
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


def _resolve_port() -> int:
    """Resolve the listen port from EMI_PORT env var (default 8000).

    Validates range; exits with a clear message on bad input rather than
    raising deep inside Werkzeug.
    """
    raw = (os.environ.get('EMI_PORT') or '').strip()
    if not raw:
        return 8000
    try:
        port = int(raw)
    except ValueError:
        print(f"\n❌ EMI_PORT={raw!r} is not an integer.\n", file=sys.stderr)
        sys.exit(1)
    if not (1 <= port <= 65535):
        print(f"\n❌ EMI_PORT={port} is out of range (1-65535).\n", file=sys.stderr)
        sys.exit(1)
    return port


def _resolve_host() -> str:
    """Resolve the bind address from EMI_HOST (default 0.0.0.0).

    The packaged launcher sets EMI_HOST=127.0.0.1 so the installed app is
    loopback-only — the server is then unreachable from the LAN (a tester's
    roommate/guest can't open the assistant, KG, or emails). Dev defaults to
    0.0.0.0 so existing tunnel / cross-device workflows are unchanged; set
    EMI_HOST=127.0.0.1 in dev too if you want loopback-only locally.

    Minimal validation: a non-empty value with no whitespace; an obviously bad
    value exits with a clear message rather than failing deep inside Werkzeug.
    """
    raw = (os.environ.get('EMI_HOST') or '').strip()
    if not raw:
        return '0.0.0.0'
    if any(c.isspace() for c in raw):
        print(f"\n❌ EMI_HOST={raw!r} contains whitespace — give a bare host/IP "
              f"(e.g. 127.0.0.1 or 0.0.0.0).\n", file=sys.stderr)
        sys.exit(1)
    return raw


def _port_is_already_serving(port: int) -> bool:
    """Probe-by-connect: True iff something on this machine accepts on
    127.0.0.1:port.

    We can't rely on bind() alone — Windows lets two processes both bind
    0.0.0.0:8000 with SO_REUSEADDR, which is exactly the duplicate-Flask
    pathology we're guarding against. Connecting catches both the
    own-process and SO_REUSEADDR cases: if anything is serving HTTP
    here, the connect succeeds.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except (ConnectionRefusedError, socket.timeout):
        return False
    except OSError:
        # Any other socket error — treat as "free" so we don't refuse to
        # start because of an unrelated networking quirk.
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _refuse_duplicate_start(port: int) -> None:
    if not _port_is_already_serving(port):
        return
    is_windows = sys.platform == "win32"
    find_pid_cmd = (
        f'netstat -ano | findstr :{port}' if is_windows
        else f'lsof -i :{port}'
    )
    kill_cmd = (
        'taskkill /PID <pid> /F' if is_windows
        else 'kill <pid>'
    )
    print(
        f"\n❌ Port {port} is already in use — another EmiOS instance is likely\n"
        f"   running. Refusing to start a second one (would double up routines,\n"
        f"   race the Ring watermark file, and contend on the SQLite DB).\n\n"
        f"   To find and stop the existing process:\n"
        f"     {find_pid_cmd}\n"
        f"     {kill_cmd}\n\n"
        f"   Or run on a different port:\n"
        f"     set EMI_PORT=8001    (Windows)\n"
        f"     export EMI_PORT=8001 (Mac/Linux)\n",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == '__main__':
    _port = _resolve_port()
    _host = _resolve_host()
    _refuse_duplicate_start(_port)

    from app.create_app import create_app

    # Startup summary (no credentials printed)
    _provider = os.environ.get('DEFAULT_LLM_PROVIDER', 'not set')
    print(
        f"EmiAi starting  |  provider={_provider}  |  "
        f"timezone={os.environ.get('TIMEZONE', 'not set')}  |  "
        f"host={_host}  |  port={_port}"
    )

    app, socketio = create_app()
    app.secret_key = os.environ['FLASK_SECRET_KEY']

    socketio.run(
        app,
        host=_host,
        port=_port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True
    )
