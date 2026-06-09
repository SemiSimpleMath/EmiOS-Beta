"""Security smoke tests for the safety kernel.

Covers: the local-only gate (loopback allowed, non-loopback AND tunnel-proxied blocked),
/dev/file hardening (.env + traversal refused), file-tool path traversal (../ and absolute-escape
refused), and registry-mutation isolation (a config read can't damage the live registry).
"""
from __future__ import annotations

import os

import pytest
from flask import Blueprint, Flask

from app.routes import _security


def _gated_client():
    app = Flask(__name__)
    bp = Blueprint("t", __name__)
    bp.before_request(_security.reject_if_not_local)

    @bp.route("/x")
    def _x():
        return "ok"

    app.register_blueprint(bp)
    return app.test_client()


# ── local-only gate ───────────────────────────────────────────────
class TestLocalGate:
    def test_loopback_allowed(self):
        r = _gated_client().get("/x", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code == 200

    def test_non_loopback_blocked(self):
        r = _gated_client().get("/x", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
        assert r.status_code == 403

    def test_tunneled_loopback_blocked(self):
        # Cloudflare tunnel: the local socket is loopback but the request carries CF headers,
        # so it originated outside this host -> must be refused.
        r = _gated_client().get(
            "/x", environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            headers={"CF-Connecting-IP": "203.0.113.5", "CF-Ray": "abc"},
        )
        assert r.status_code == 403

    def test_xforwarded_loopback_blocked(self):
        r = _gated_client().get(
            "/x", environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        assert r.status_code == 403


# ── /dev/file hardening ───────────────────────────────────────────
class TestDevFile:
    def _client(self):
        import app.routes.subsystem_route as sr
        app = Flask(__name__)
        app.register_blueprint(sr.subsystem_route_bp)
        return app.test_client()

    def test_dotenv_blocked(self):
        # Local request (default loopback, no proxy headers) so the gate passes; .env must still 403
        # (repo root is not in the artifact allowlist, and the secret suffix is denied).
        r = self._client().get("/dev/file/.env", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code == 403

    def test_db_blocked(self):
        r = self._client().get("/dev/file/emi.db", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code == 403

    def test_traversal_blocked(self):
        r = self._client().get(
            "/dev/file/../../Windows/System32/drivers/etc/hosts",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert r.status_code in (403, 404)

    def test_tunneled_blocked_by_gate(self):
        r = self._client().get(
            "/dev/file/.env",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            headers={"CF-Connecting-IP": "203.0.113.5"},
        )
        assert r.status_code == 403


# ── file-tool path traversal ──────────────────────────────────────
class TestFileToolTraversal:
    def test_read_tool_refuses_relative_escape(self):
        from app.assistant.lib.tools.read_text_file.read_text_file import _resolve_path
        assert _resolve_path("../../etc/passwd") is None
        assert _resolve_path("/etc/passwd") is None          # absolute outside repo
        assert _resolve_path("README.md") is not None         # repo-relative resolves

    def test_append_tool_refuses_relative_escape(self):
        from app.assistant.lib.tools.append_text_file.append_text_file import _resolve_path
        assert _resolve_path("../../tmp/evil.txt") is None
        assert _resolve_path("/tmp/evil.txt") is None


# ── registry-mutation isolation ───────────────────────────────────
class TestRegistryCopy:
    def test_get_agent_config_returns_copy(self):
        from app.assistant.agent_registry.agent_registry import AgentRegistry
        reg = AgentRegistry()
        reg.configs = {"a": {"class": object(), "structured_output": True, "x": 1}}
        cfg = reg.get_agent_config("a")
        cfg.pop("class")
        cfg.pop("structured_output")
        # The live registry config must be untouched by the caller's pops.
        assert "class" in reg.configs["a"]
        assert "structured_output" in reg.configs["a"]


# ── /api/shutdown gate ────────────────────────────────────────────
class TestShutdownGate:
    def _client(self):
        import app.routes.health_check as hc
        app = Flask(__name__)
        app.register_blueprint(hc.health_check_bp)
        return app.test_client()

    # NOTE: only the BLOCKED paths are exercised — a successful (loopback) shutdown would os.kill the
    # test process. The decorator's allow-path is covered by TestLocalGate.

    def test_shutdown_blocked_non_local(self):
        r = self._client().post("/api/shutdown", environ_overrides={"REMOTE_ADDR": "192.168.1.50"})
        assert r.status_code == 403

    def test_shutdown_blocked_tunneled(self):
        r = self._client().post(
            "/api/shutdown",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            headers={"CF-Connecting-IP": "203.0.113.5"},
        )
        assert r.status_code == 403


# ── named path helpers ────────────────────────────────────────────
class TestPathHelpers:
    def test_artifact_child_rejects_repo_root_and_traversal(self):
        from app.assistant.utils.path_utils import resolve_artifact_child
        with pytest.raises(ValueError):
            resolve_artifact_child(".env")               # repo root is NOT an artifact dir
        with pytest.raises(ValueError):
            resolve_artifact_child("../../etc/passwd")    # traversal

    def test_artifact_child_allows_data_path(self):
        from app.assistant.utils.path_utils import resolve_artifact_child, get_data_dir
        candidate = get_data_dir().resolve() / "images" / "x.png"
        assert resolve_artifact_child(candidate) == candidate.resolve()

    def test_data_child_rejects_outside_data(self):
        from app.assistant.utils.path_utils import resolve_data_child
        with pytest.raises(ValueError):
            resolve_data_child(".env")
        with pytest.raises(ValueError):
            resolve_data_child("../../etc/passwd")

    def test_data_child_allows_data_path(self):
        from app.assistant.utils.path_utils import resolve_data_child, get_data_dir
        candidate = get_data_dir().resolve() / "images" / "x.png"
        assert resolve_data_child(candidate) == candidate.resolve()


# ── regression guard: no ad-hoc startswith path checks ────────────
class TestNoAdhocStartswith:
    def test_no_startswith_str_path_checks_in_app(self):
        """Ban str.startswith(str(...)) containment checks across app/ (a sibling like <root>_evil
        defeats them) — use resolve_repo_child / resolve_data_child / resolve_artifact_child /
        relative_to instead. Tests are excluded (they may reference the banned pattern as a string)."""
        from app.assistant.utils.path_utils import get_repo_root
        app_dir = get_repo_root() / "app"
        needle = ".startswith(str("
        offenders = []
        for root, dirs, files in os.walk(app_dir):
            dirs[:] = [d for d in dirs if d not in {"__pycache__", "tests", "test", "node_modules"}]
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        text = fh.read()
                except Exception:
                    continue
                if needle in text:
                    offenders.append(os.path.relpath(fpath, app_dir))
        assert offenders == [], (
            f"ad-hoc startswith(str(...)) path checks found (use resolve_*_child / relative_to): {offenders}"
        )
