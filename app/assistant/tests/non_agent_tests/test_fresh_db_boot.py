"""Fresh-DB first-boot smoke test — schema must build on a brand-new EMI_DATA_DIR.

Regression guard for the installer bug: a packaged install boots against an EMPTY
EMI_DATA_DIR with no prior `python setup.py`, so the app builds its own schema on
first boot via create_app() -> initialize_all_tables(). That path crashed with
`NoReferencedTableError: ... entity_card_v2.entity_node_id could not find table
kg_node_metadata` because the always-on entity-card create_all ran before the KG
models were registered. The dev flow masked it (setup.py imports the full model set
first). Fix: initialize_all_tables() now registers ALL models before any create_all.

This test reproduces a fresh first boot in a SUBPROCESS (the engine + EMI_DATA_DIR
are process-global, so a clean child with a fresh data dir is the only faithful way)
and asserts the FK-bearing tables get created without error — i.e. the exact
create_app DB-init path a new install hits. The full packaged-bundle boot
(/api/version -> 200) is exercised separately by the packaging harness.

Run:
  .venv\\Scripts\\python.exe -m pytest app/assistant/tests/non_agent_tests/test_fresh_db_boot.py
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

# Runs in a clean child process pointed at a brand-new EMI_DATA_DIR. Mirrors how
# create_app() initializes the DB (inside a Flask app context — music tables use
# Flask-SQLAlchemy's db.engine, which requires one), then asserts the schema built.
_BOOT = textwrap.dedent(
    """
    import os, sys, tempfile
    os.environ["EMI_DATA_DIR"] = tempfile.mkdtemp(prefix="emi_freshdb_")
    os.environ.pop("DEV_DATABASE_URI_EMI", None)
    os.environ.pop("USE_TEST_DB", None)
    sys.path.insert(0, ".")

    from flask import Flask
    from app.assistant.database.db_instance import db
    from app.models.base import get_database_uri, get_current_engine

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = get_database_uri()
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        from app.database.table_initializer import initialize_all_tables
        initialize_all_tables()  # the create_app:86 path that crashed on a fresh DB
        from sqlalchemy import inspect
        names = set(inspect(get_current_engine()).get_table_names())

    # entity_card_v2 + kg_node_metadata are the FK pair the bug tripped on; the
    # others confirm core/news groups also built on the fresh DB.
    required = {"entity_card_v2", "kg_node_metadata", "news_articles", "unified_log_2026"}
    missing = sorted(required - names)
    assert not missing, f"fresh DB missing tables: {missing}"
    print(f"FRESH_DB_OK tables={len(names)}")
    """
)


def _find_repo_root() -> Path:
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "run_flask.py").exists():
            return parent
    raise RuntimeError("could not locate repo root (run_flask.py marker)")


def test_fresh_db_first_boot_builds_schema():
    repo_root = _find_repo_root()
    venv_py = repo_root / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    py = str(venv_py) if venv_py.exists() else sys.executable

    proc = subprocess.run(
        [py, "-c", _BOOT],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert "FRESH_DB_OK" in proc.stdout, (
        "fresh-DB first boot failed to build the schema.\n"
        f"--- STDOUT ---\n{proc.stdout}\n--- STDERR (tail) ---\n{proc.stderr[-3000:]}"
    )
    assert proc.returncode == 0, f"boot exited {proc.returncode}\n{proc.stderr[-2000:]}"


# Full fresh-install boot: create_app() against a clean EMI_DATA_DIR must reach a
# SERVING server (GET /api/version -> 200), not just build tables. Guards the
# Google/config blockers (config templates seeded at boot; oauth_registry tolerates a
# missing file; account_ids defers unconfigured default accounts).
_BOOT_FULL = textwrap.dedent(
    """
    import os, sys, tempfile
    # Clean-install env: fresh data dir + strip any account/db env the runner may carry.
    for k in list(os.environ):
        if k.startswith("EMI_GOOGLE") or k in ("EMI_DATA_DIR",) or k.startswith("EMI_CONFIGS") or k.startswith("DEV_DATABASE_URI"):
            os.environ.pop(k, None)
    os.environ["EMI_DATA_DIR"] = tempfile.mkdtemp(prefix="emi_freshboot_")
    os.environ.setdefault("FLASK_SECRET_KEY", "test_" + os.urandom(8).hex())
    os.environ.setdefault("JWT_SECRET_KEY", "test_" + os.urandom(8).hex())
    sys.path.insert(0, ".")
    import os.path as p

    from app.create_app import create_app
    app, _socketio = create_app()                 # the create_app:86 init path, full boot
    r = app.test_client().get("/api/version")
    assert r.status_code == 200, f"/api/version -> {r.status_code}"
    cfg = p.join(os.environ["EMI_DATA_DIR"], "configs")
    assert p.exists(p.join(cfg, "oauth_accounts.json")), "oauth_accounts.json not seeded"
    print(f"FRESH_BOOT_OK status={r.status_code} version={r.get_json()}")
    sys.stdout.flush()
    os._exit(0)  # create_app started background threads; don't wait on them
    """
)


def test_fresh_install_boots_to_serving_server():
    repo_root = _find_repo_root()
    venv_py = repo_root / ".venv" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    py = str(venv_py) if venv_py.exists() else sys.executable

    proc = subprocess.run(
        [py, "-c", _BOOT_FULL],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert "FRESH_BOOT_OK" in proc.stdout, (
        "fresh install did not boot to a serving server.\n"
        f"--- STDOUT (tail) ---\n{proc.stdout[-2000:]}\n--- STDERR (tail) ---\n{proc.stderr[-4000:]}"
    )


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
