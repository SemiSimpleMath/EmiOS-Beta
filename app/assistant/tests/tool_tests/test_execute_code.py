"""Tests for execute_code — Docker-sandboxed Python execution.

These tests mock `subprocess.run` so they do not require Docker to be running.
Live-container tests live in tests/integration/ and are gated on Docker
availability.

Coverage:
  - invalid_arguments (empty source) — pre-flight rejected
  - unsupported_language — pre-flight rejected
  - timeout_too_large — pre-flight rejected
  - docker_not_installed — subprocess FileNotFoundError mapped cleanly
  - Successful run — stdout returned, exit code 0
  - Non-zero exit — error_code surfaced
  - Timeout — subprocess.TimeoutExpired → ToolResult with error_code=timeout
  - egress_allowlist gates --network flag (none vs bridge)
  - fs_allowlist becomes read-only mounts
  - requirements changes entrypoint to `sh -c pip install ... && python ...`
  - Output files in workspace/outputs/ get minted as pods
  - response_pod_kind seals stdout (stdout NOT in result.data)
  - sandbox_audit row written
"""
from __future__ import annotations

import os
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import app.assistant.tests.test_setup  # noqa: F401  bootstraps DI

from app.assistant.lib.tools.execute_code.execute_code import ExecuteCode
from app.assistant.utils.pydantic_classes import ToolMessage


def _tm(args: dict, request_id: str = "req-exec-1") -> ToolMessage:
    return ToolMessage(
        request_id=request_id,
        tool_name="execute_code",
        tool_data={"arguments": args},
    )


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class TestExecuteCodePreflight(unittest.TestCase):
    """Argument validation runs before the workspace is created. These never
    touch subprocess."""

    def test_empty_source_rejected(self):
        result = ExecuteCode().execute(_tm({"source": ""}))
        self.assertEqual(result.result_type, "error")
        self.assertEqual(result.data["error_code"], "invalid_arguments")

    def test_whitespace_only_source_rejected(self):
        result = ExecuteCode().execute(_tm({"source": "   \n  \t  "}))
        self.assertEqual(result.data["error_code"], "invalid_arguments")

    def test_unsupported_language_rejected(self):
        result = ExecuteCode().execute(
            _tm({"source": "console.log('hi')", "language": "javascript"})
        )
        self.assertEqual(result.data["error_code"], "unsupported_language")

    def test_timeout_above_hard_cap_rejected(self):
        result = ExecuteCode().execute(
            _tm({"source": "print(1)", "timeout_s": 999.0})
        )
        self.assertEqual(result.data["error_code"], "timeout_too_large")


class TestExecuteCodeMockedDocker(unittest.TestCase):
    """Happy path + error mapping with subprocess.run mocked."""

    def test_docker_not_installed_maps_cleanly(self):
        tool = ExecuteCode()
        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=FileNotFoundError("[Errno 2] docker"),
        ):
            result = tool.execute(_tm({"source": "print('hi')"}))
        self.assertEqual(result.data["error_code"], "docker_not_installed")

    def test_successful_run_returns_stdout(self):
        tool = ExecuteCode()
        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            return_value=_FakeProc(stdout=b"hello world\n", returncode=0),
        ):
            result = tool.execute(_tm({"source": "print('hello world')"}))

        self.assertEqual(result.result_type, "execute_code")
        self.assertTrue(result.data["ok"])
        self.assertEqual(result.data["exit_code"], 0)
        self.assertEqual(result.data["stdout"], "hello world\n")
        self.assertEqual(result.data["stdout_bytes"], 12)

    def test_nonzero_exit_surfaces_stderr(self):
        tool = ExecuteCode()
        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            return_value=_FakeProc(
                stdout=b"",
                stderr=b"Traceback (most recent call last): NameError: foo",
                returncode=1,
            ),
        ):
            result = tool.execute(_tm({"source": "print(foo)"}))

        self.assertFalse(result.data["ok"])
        self.assertEqual(result.data["exit_code"], 1)
        self.assertIn("NameError", result.data["stderr"])

    def test_timeout_returns_timeout_error_code(self):
        tool = ExecuteCode()
        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker run", timeout=30.0),
        ), patch(
            "app.assistant.lib.tools.execute_code.execute_code.ExecuteCode._kill_container_by_name"
        ):
            result = tool.execute(_tm({"source": "while True: pass", "timeout_s": 0.5}))

        self.assertEqual(result.result_type, "error")
        self.assertEqual(result.data["error_code"], "timeout")

    def test_default_no_egress_uses_network_none(self):
        """No egress_allowlist → --network=none in the docker run argv."""
        tool = ExecuteCode()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(stdout=b"ok\n", returncode=0)

        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=fake_run,
        ):
            tool.execute(_tm({"source": "print('ok')"}))

        self.assertIn("--network=none", captured["cmd"])

    def test_egress_allowlist_drops_network_none(self):
        """Non-empty egress_allowlist → default bridge network (no --network=none)."""
        tool = ExecuteCode()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(stdout=b"ok\n", returncode=0)

        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=fake_run,
        ):
            tool.execute(
                _tm({
                    "source": "import urllib.request; print('ok')",
                    "egress_allowlist": ["api.openai.com"],
                })
            )

        self.assertNotIn("--network=none", captured["cmd"])

    def test_requirements_changes_entrypoint(self):
        """Non-empty requirements → entrypoint is `sh -c pip install ... && python ...`."""
        tool = ExecuteCode()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(stdout=b"ok\n", returncode=0)

        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=fake_run,
        ):
            tool.execute(
                _tm({
                    "source": "import faster_whisper; print('loaded')",
                    "requirements": ["faster-whisper"],
                })
            )

        self.assertIn("--entrypoint", captured["cmd"])
        self.assertIn("sh", captured["cmd"])
        shell_cmd_str = " ".join(captured["cmd"])
        self.assertIn("pip install --no-cache-dir", shell_cmd_str)
        self.assertIn("faster-whisper", shell_cmd_str)
        self.assertIn("python /workspace/script.py", shell_cmd_str)

    def test_fs_allowlist_mounts_readonly(self):
        """fs_allowlist paths → read-only -v mounts at /workspace/host_<basename>."""
        tool = ExecuteCode()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeProc(stdout=b"ok\n", returncode=0)

        # Use a real path that exists (this test file itself).
        test_path = str(Path(__file__).resolve())

        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=fake_run,
        ):
            tool.execute(
                _tm({
                    "source": "import os; print(os.listdir('/workspace'))",
                    "fs_allowlist": [test_path],
                })
            )

        cmd_str = " ".join(captured["cmd"])
        self.assertIn(":ro", cmd_str)
        self.assertIn("test_execute_code.py", cmd_str)

    def test_response_pod_kind_seals_stdout(self):
        """When response_pod_kind is set, stdout goes into a pod and IS NOT
        returned inline in result.data."""
        tool = ExecuteCode()
        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            return_value=_FakeProc(stdout=b"SENSITIVE health data\n", returncode=0),
        ):
            result = tool.execute(
                _tm({
                    "source": "print('SENSITIVE health data')",
                    "response_pod_kind": "health.private",
                })
            )

        self.assertNotIn("stdout", result.data)  # sealed; not inline
        self.assertIn("response_pod_id", result.data)
        self.assertEqual(result.data["response_pod_kind"], "health.private")
        # Token MUST NOT appear in the result fields
        self.assertNotIn("SENSITIVE", str(result.data))

    def test_output_files_become_pods(self):
        """Files written to workspace/outputs/ during the (mocked) run get
        minted as pods returned in output_pod_ids."""
        tool = ExecuteCode()

        def fake_run(cmd, **kwargs):
            # Find the first -v whose right side is :/workspace (the workspace
            # bind-mount; fs_allowlist mounts end in :/workspace/host_*:ro).
            # On Windows the host path itself contains a colon ("E:\..."),
            # so rsplit on the final colon to isolate it.
            for i, tok in enumerate(cmd):
                if tok != "-v" or i + 1 >= len(cmd):
                    continue
                spec = cmd[i + 1]
                host_part, sep, container_part = spec.rpartition(":")
                if not host_part or container_part != "/workspace":
                    continue
                outdir = Path(host_part) / "outputs"
                if outdir.exists():
                    (outdir / "result.txt").write_text("computed=42")
                    break
            return _FakeProc(stdout=b"done\n", returncode=0)

        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            side_effect=fake_run,
        ):
            result = tool.execute(
                _tm({"source": "open('outputs/result.txt','w').write('computed=42')"})
            )

        self.assertEqual(len(result.data["output_pod_ids"]), 1)
        self.assertTrue(
            result.data["output_pod_ids"][0].startswith("datapod:tool_result:")
        )

    def test_audit_row_written_on_success(self):
        """One sandbox_audit row per successful call. Sizes recorded; full
        stdout/stderr NEVER stored."""
        from app.assistant.lib.tools.execute_code.models import SandboxAudit
        from app.models.base import get_session

        tool = ExecuteCode()
        request_id = f"req-audit-success-{uuid.uuid4().hex}"
        with patch(
            "app.assistant.lib.tools.execute_code.execute_code.subprocess.run",
            return_value=_FakeProc(stdout=b"foo bar baz\n", returncode=0),
        ):
            tool.execute(_tm({"source": "print('foo bar baz')"}, request_id=request_id))

        session = get_session()
        try:
            row = (
                session.query(SandboxAudit)
                .filter_by(request_id=request_id)
                .first()
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.exit_code, 0)
            self.assertEqual(row.stdout_bytes, 12)
            self.assertEqual(row.language, "python")
            # source_hash is sha256, 64 hex chars; never the raw source
            self.assertEqual(len(row.source_hash), 64)
        finally:
            session.close()

    def test_audit_row_written_on_preflight_failure(self):
        """Preflight failures also get audited so we can see misuse patterns."""
        from app.assistant.lib.tools.execute_code.models import SandboxAudit
        from app.models.base import get_session

        request_id = f"req-audit-failure-{uuid.uuid4().hex}"
        ExecuteCode().execute(
            _tm({"source": "", "language": "python"}, request_id=request_id)
        )

        session = get_session()
        try:
            row = (
                session.query(SandboxAudit)
                .filter_by(request_id=request_id)
                .first()
            )
            self.assertIsNotNone(row)
            self.assertEqual(row.error_code, "invalid_arguments")
            self.assertIsNone(row.exit_code)
        finally:
            session.close()


def _docker_available() -> bool:
    """Probe whether Docker is installed AND the daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=5.0, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _sandbox_image_present() -> bool:
    try:
        result = subprocess.run(
            ["docker", "images", "-q", "emi-sandbox:v1"],
            capture_output=True, timeout=5.0, check=False,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except Exception:
        return False


@unittest.skipUnless(
    _docker_available() and _sandbox_image_present(),
    "Live container smoke test — needs Docker daemon up + `emi-sandbox:v1` built. "
    "Build with: docker build -t emi-sandbox:v1 docker/sandbox/",
)
class TestExecuteCodeIntegration(unittest.TestCase):
    """Real container smoke. Auto-skipped when Docker isn't running."""

    def test_hello_world_runs_in_container(self):
        result = ExecuteCode().execute(_tm({"source": "print('hello from sandbox')"}))
        self.assertTrue(result.data["ok"], msg=str(result.data))
        self.assertEqual(result.data["exit_code"], 0)
        self.assertIn("hello from sandbox", result.data["stdout"])

    def test_pandas_available_in_base_image(self):
        result = ExecuteCode().execute(_tm({
            "source": "import pandas; print(pandas.__version__)",
        }))
        self.assertTrue(result.data["ok"], msg=str(result.data))
        # Pinned to 2.2.3 in the Dockerfile.
        self.assertIn("2.2.3", result.data["stdout"])

    def test_network_denied_by_default(self):
        result = ExecuteCode().execute(_tm({
            "source": (
                "import urllib.request, sys\n"
                "try:\n"
                "    urllib.request.urlopen('https://api.github.com', timeout=2)\n"
                "    print('REACHED'); sys.exit(0)\n"
                "except Exception as e:\n"
                "    print(f'BLOCKED: {type(e).__name__}'); sys.exit(1)\n"
            ),
        }))
        # With --network=none, the URL open fails. We expect exit=1, stdout
        # containing BLOCKED.
        self.assertEqual(result.data["exit_code"], 1)
        self.assertIn("BLOCKED", result.data["stdout"])

    def test_output_file_minted_as_pod(self):
        result = ExecuteCode().execute(_tm({
            "source": (
                "with open('/workspace/outputs/sum.txt', 'w') as f:\n"
                "    f.write('answer=42')\n"
                "print('wrote sum.txt')\n"
            ),
        }))
        self.assertTrue(result.data["ok"], msg=str(result.data))
        self.assertEqual(len(result.data["output_pod_ids"]), 1)


if __name__ == "__main__":
    unittest.main()
