"""Security smoke tests for the safety kernel.

Covers: the local-only gate (loopback allowed, non-loopback AND tunnel-proxied blocked),
/dev/file hardening (.env + traversal refused), file-tool path traversal (../ and absolute-escape
refused), and registry-mutation isolation (a config read can't damage the live registry).
"""
from __future__ import annotations

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
