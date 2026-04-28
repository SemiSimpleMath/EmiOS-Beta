import sys
import time
import subprocess
import os
from pathlib import Path

# -----------------------------
# Test settings (local-only)
# -----------------------------
# Print rendered system/user prompts to stdout for debugging.
# (This is read by Agent.call_llm; setting it here avoids shell env changes.)
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
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


_HERE = Path(__file__).resolve()
_REPO_ROOT = None
for parent in _HERE.parents:
    if (parent / "app").is_dir():
        _REPO_ROOT = parent
        break
if _REPO_ROOT is None:
    raise RuntimeError(f"Could not locate repo root from: {_HERE}")


def _refresh_playwright_mcp_tool_cache() -> tuple[int, str, str]:
    """
    Best-effort helper to populate `mcp/tool_cache/npm__playwright-mcp.json` for live runs.

    We keep this in the test runner to avoid surprising side effects in core ToolRegistry.
    """
    script = _REPO_ROOT / "mcp" / "refresh_tool_cache.py"
    cmd = [
        sys.executable,
        str(script),
        "--server-id",
        "npm/playwright-mcp",
        "--launch-id",
        "cmd_npx",
        # First-time `npx` runs can take a while (download + startup).
        "--timeout",
        "60",
    ]
    logger.warning(
        "Playwright MCP tools missing; attempting to refresh tool cache by running:\n"
        + "  " + " ".join(cmd)
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def main():
    logger.info("Running Playwright manager test (live DoorDash)...")
    factory = DI.multi_agent_manager_factory

    preload_start = time.time()
    DI.manager_registry.preload_all()
    logger.info(f"✅ Preloading completed in {time.time() - preload_start:.2f}s")

    # Fail fast if config unexpectedly reverted (common after IDE undo / long-running processes).
    try:
        cfg = DI.manager_registry.get("playwright_manager") or {}
        cfg_max = cfg.get("max_cycles")
        logger.warning(f"[playwright_manager_test] Registry config max_cycles={cfg_max!r}")
    except Exception:
        pass

    # Load curated MCP servers + cached tools.
    DI.tool_registry.load_mcp_servers()
    DI.tool_registry.load_mcp_tool_cache(enabled_only=True)

    # Create the manager
    manager = factory.create_manager("playwright_manager")
    try:
        mgr_max = (getattr(manager, "manager_config", {}) or {}).get("max_cycles")
        logger.warning(f"[playwright_manager_test] Manager instance max_cycles={mgr_max!r}")
        if int(mgr_max or 0) < 50:
            raise RuntimeError(
                f"playwright_manager max_cycles is unexpectedly low ({mgr_max!r}). "
                "Expected 100. Did an old config get cached, or did an orchestrator budget override it?"
            )
    except Exception as e:
        raise

    # Fail fast if Playwright MCP tools aren't registered (cache/allowlist mismatch).
    required = [
        "mcp::npm/playwright-mcp::browser_navigate",
        "mcp::npm/playwright-mcp::browser_snapshot",
        "mcp::npm/playwright-mcp::browser_click",
        "mcp::npm/playwright-mcp::browser_wait_for",
        "mcp::npm/playwright-mcp::browser_take_screenshot",
        "mcp::npm/playwright-mcp::browser_tabs",
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

    #task = """Go to dailykos.com and find an article and click into it."""
    # task = """Go to DoorDash and order from Thai Spice. Get two orders of yellow chicken curry with potatoes.  When you hit + to add to cart you will have a pop up modal to make choices,
    # order 1) with medium spice, 2) white rice 3) on the side. When you are in the modal you may have to scroll down slightly to see all the options, you may have to scroll multiple times slightly.  Modal may need focus in order to be able to be scrolled, mouse has to be at least moved on top of the modal to scrooll it.  Only after you have picked all the required choises will you be able to finnish add to cart.  Get two orders."
    #         "Proceed to checkout, but STOP before placing the final order"""

    task = """Goto Doordash and search for Carl's Jr in the search bar on top order from recent orders Double Western Bacon Cheeseburger and Jalapeno poppers. Add to cart. Stop before going to check out and just wait."""

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

