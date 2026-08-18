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

# ── Reverse proxy support (Caddy / nginx / Tailscale Serve) ────────────────
# When EMI_PROXY_SUBPATH is set (e.g. "/emi"), Flask generates correct URLs
# behind a subpath reverse proxy. Pair with a proxy that strips the prefix
# and sets X-Forwarded-* headers.
_emi_proxy_path = os.environ.get("EMI_PROXY_SUBPATH", "").strip().rstrip("/")
if _emi_proxy_path:
    os.environ.setdefault("SCRIPT_NAME", _emi_proxy_path)

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


def _seed_runtime_configs() -> None:
    """Copy default configs from the repo/image into the persistent volume.

    At runtime get_configs_dir() resolves to <EMI_DATA_DIR>/configs (e.g.
    /data/configs in Docker) so that users can edit them persistently, but a
    fresh volume contains none of the shipped defaults (subsystems.yaml,
    routines.json, routines/, model_tiers.yaml, ...). Without this seed the
    routine manager finds 0 routines, subsystem flags default to disabled,
    and every widget-producing scheduled task silently never runs.

    Only missing files are copied — existing files are never overwritten, so
    user edits to a config survive restarts. Run before any app import that
    calls get_configs_dir().
    """
    import shutil

    from pathlib import Path as _Path

    try:
        source = _Path(__file__).resolve().parent / "configs"
        if not source.is_dir():
            print(f"⚠ config seed: no default configs at {source}", file=sys.stderr)
            return
        data_dir = os.environ.get("EMI_DATA_DIR")
        if not data_dir:
            return  # dev mode: get_configs_dir already points at repo configs
        dest = _Path(data_dir) / "configs"
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src_file in sorted(source.rglob("*")):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(source)
            dst_file = dest / rel
            if dst_file.exists():
                continue  # never clobber user edits
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)
            copied += 1
        if copied:
            print(f"✔ config seed: copied {copied} default config(s) to {dest}")
        else:
            print("✔ config seed: volume already has all default configs")
    except Exception as e:
        # A half-seeded configs volume boots into silently-missing routines and
        # disabled subsystems — refuse to start instead of warning and limping on.
        print(f"✗ config seed FAILED: {e}", file=sys.stderr)
        raise


_seed_runtime_configs()


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


def _find_incumbents(port: int) -> dict:
    """Live EmiOS processes other than ourselves: any python running this
    project's run_flask.py / start.py (by cmdline or cwd), plus whatever is
    LISTENING on our port. Excludes this process and its ancestors (start.py
    launches run_flask.py as a child — the parent is not an incumbent).

    This exists because orphaned instances are invisible: 2026-08-02 found a
    run_flask pair alive since 07-31 and start.py launchers alive since 07-15,
    silently holding the port while every user restart no-op'd against them.
    """
    import psutil

    self_and_ancestors = {os.getpid()}
    try:
        for ancestor in psutil.Process().parents():
            self_and_ancestors.add(ancestor.pid)
    except psutil.Error:
        pass

    project = str(Path(__file__).resolve().parent).lower()
    incumbents: dict = {}
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid in self_and_ancestors:
                continue
            if "python" not in (proc.info.get("name") or "").lower():
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            low = cmdline.lower().replace("/", "\\")
            if "run_flask.py" not in low and "start.py" not in low:
                continue
            in_project = project in low
            if not in_project:
                try:
                    in_project = str(proc.cwd()).lower() == project
                except psutil.Error:
                    in_project = False
            if in_project:
                incumbents[proc.pid] = cmdline
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    try:
        for conn in psutil.net_connections(kind="tcp"):
            if (conn.laddr and conn.laddr.port == port
                    and conn.status == psutil.CONN_LISTEN and conn.pid
                    and conn.pid not in self_and_ancestors
                    and conn.pid not in incumbents):
                try:
                    incumbents[conn.pid] = " ".join(psutil.Process(conn.pid).cmdline())
                except psutil.Error:
                    incumbents[conn.pid] = f"(pid {conn.pid} listening on :{port})"
    except psutil.Error:
        pass
    return incumbents


def _handle_duplicate_start(port: int, *, replace: bool) -> None:
    """Single-instance guard. Default: refuse loudly, naming the live PIDs.
    ``--replace``: terminate the incumbents, verify the port is free, take over
    — so a restart is one gesture instead of taskkill archaeology."""
    incumbents = _find_incumbents(port)
    serving = _port_is_already_serving(port)
    if not incumbents and not serving:
        return

    if not replace:
        listing = "\n".join(
            f"     PID {pid}: {cmd[:110]}" for pid, cmd in sorted(incumbents.items())
        ) or f"     (port {port} is served but the owner could not be identified)"
        print(
            f"\n❌ EmiOS is already running — refusing to start a second instance\n"
            f"   (would double up routines and contend on the SQLite DB).\n"
            f"   Live EmiOS processes:\n{listing}\n\n"
            f"   To restart (stops the old instance first):\n"
            f"     restart.bat                          (Windows)\n"
            f"     python run_flask.py --replace\n",
            file=sys.stderr,
        )
        sys.exit(1)

    import time
    import psutil

    print(f"--replace: stopping {len(incumbents)} existing EmiOS process(es): "
          f"{sorted(incumbents)}")
    procs = []
    for pid in incumbents:
        try:
            procs.append(psutil.Process(pid))
        except psutil.Error:
            pass
    for proc in procs:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(procs, timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=5)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _port_is_already_serving(port):
        time.sleep(0.3)
    if _port_is_already_serving(port):
        print(f"\n❌ --replace could not free port {port} — something still "
              f"serves it. Not starting a duplicate.\n", file=sys.stderr)
        sys.exit(1)
    print("--replace: old instance stopped; taking over.")


if __name__ == '__main__':
    _port = _resolve_port()
    _host = _resolve_host()
    _handle_duplicate_start(_port, replace="--replace" in sys.argv[1:])

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
