import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# -----------------------------
# Test settings (local-only)
# -----------------------------
PRINT_RENDERED_PROMPTS = True
if PRINT_RENDERED_PROMPTS:
    os.environ["EMI_PRINT_PROMPTS"] = "1"

# Ensure repo root is on sys.path so `import app...` works when running by file path.
_HERE = Path(__file__).resolve()
for parent in _HERE.parents:
    if (parent / "app").is_dir():
        sys.path.insert(0, str(parent))
        break

import app.assistant.tests.test_setup  # noqa: F401 (side-effect: initializes DI)
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.pydantic_classes import Message

logger = get_logger(__name__)

_HERE = Path(__file__).resolve()
_REPO_ROOT = None
for parent in _HERE.parents:
    if (parent / "app").is_dir():
        _REPO_ROOT = parent
        break
if _REPO_ROOT is None:
    raise RuntimeError(f"Could not locate repo root from: {_HERE}")

TEST_URL = "http://127.0.0.1:8000/playwright_modal_test"


def _refresh_playwright_mcp_tool_cache() -> tuple[int, str, str]:
    """
    Best-effort helper to populate `mcp/tool_cache/npm__playwright-mcp.json` for live runs.
    """
    script = _REPO_ROOT / "mcp" / "refresh_tool_cache.py"
    cmd = [
        sys.executable,
        str(script),
        "--server-id",
        "npm/playwright-mcp",
        "--launch-id",
        "cmd_npx",
        "--timeout",
        "60",
    ]
    logger.warning(
        "Playwright MCP tools missing; attempting to refresh tool cache by running:\n"
        + "  "
        + " ".join(cmd)
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def _assert_modal_test_page_live(url: str) -> None:
    candidates = [url]
    if not url.endswith("/"):
        candidates.append(url + "/")

    statuses: list[str] = []
    for candidate in candidates:
        try:
            with urlopen(candidate, timeout=3.0) as resp:
                status = int(getattr(resp, "status", 0) or 0)
                body = resp.read(32768).decode("utf-8", errors="replace")
            marker_ok = (
                ("Playwright Modal Scroll Test" in body)
                or ('data-testid="customize-modal"' in body)
                or ("playwright_modal_test.html" in body)
            )
            statuses.append(f"{candidate} -> HTTP {status}, marker={marker_ok}")
            if status == 200 and marker_ok:
                return
        except HTTPError as e:
            statuses.append(f"{candidate} -> HTTPError {e.code}")
        except URLError as e:
            statuses.append(f"{candidate} -> URLError {e}")
        except Exception as e:
            logger.debug("Modal page liveness check exception details", exc_info=True)
            statuses.append(f"{candidate} -> Exception {e}")

    details = "\n".join(f"- {s}" for s in statuses) if statuses else "- no checks attempted"
    raise RuntimeError(
        "Cannot verify modal test page route.\n"
        f"Tried:\n{details}\n\n"
        "Start or restart Flask from this repo (run_flask.py) on port 8000."
    )


def main():
    logger.info("Running Playwright manager modal test (deterministic modal sandbox)...")
    _assert_modal_test_page_live(TEST_URL)
    test_url = TEST_URL
    logger.info("Using modal test route: %s", test_url)
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    DI.manager_registry.preload_all()
    logger.info(f"✅ Preloading completed in {time.time() - preload_start:.2f}s")

    # Load curated MCP servers + cached tools.
    DI.tool_registry.load_mcp_servers()
    DI.tool_registry.load_mcp_tool_cache(enabled_only=True)

    # Create manager
    manager = factory.create_manager("playwright_manager")
    mgr_max = (getattr(manager, "manager_config", {}) or {}).get("max_cycles")
    logger.warning(f"[playwright_modal_test] Manager instance max_cycles={mgr_max!r}")

    # Fail fast if required MCP tools aren't registered (cache mismatch).
    required = [
        "mcp::npm/playwright-mcp::browser_navigate",
        "mcp::npm/playwright-mcp::browser_wait_for",
        "mcp::npm/playwright-mcp::browser_tabs",
        "mcp::npm/playwright-mcp::browser_mouse_click_xy",
        "mcp::npm/playwright-mcp::browser_take_screenshot",
    ]
    missing = [t for t in required if not DI.tool_registry.get_tool(t)]
    if missing:
        code, out, err = _refresh_playwright_mcp_tool_cache()
        if code != 0:
            raise RuntimeError(
                "Missing Playwright MCP tools in ToolRegistry:\n"
                + "\n".join(f"- {t}" for t in missing)
                + "\n\nAutomatic cache refresh failed.\n"
                + f"- exit_code: {code}\n"
                + (f"\n[stdout]\n{out.strip()}\n" if out.strip() else "")
                + (f"\n[stderr]\n{err.strip()}\n" if err.strip() else "")
            )
        DI.tool_registry.load_mcp_tool_cache(enabled_only=True)
        missing = [t for t in required if not DI.tool_registry.get_tool(t)]
        if missing:
            raise RuntimeError(
                "Missing Playwright MCP tools in ToolRegistry (even after refresh):\n"
                + "\n".join(f"- {t}" for t in missing)
            )

    task = (
        f"Navigate to {test_url}. "
        "The customization modal is open. You must complete this exact flow: "
        "1) select Medium spice, "
        "2) select White rice, "
        "3) select On the side, "
        "4) click Add to cart at the bottom. "
        "Return control only after Add to cart is clicked and success text is visible."
    )
    request_message = Message(
        data_type="agent_activation",
        sender="User",
        receiver="Delegator",
        content="",
        task=task,
        information="",
    )
    result = manager.request_handler(request_message)
    print(result)


if __name__ == "__main__":
    main()

