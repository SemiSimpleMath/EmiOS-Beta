from pydantic import BaseModel


class playwright_browse_args(BaseModel):
    # Target page
    url: str

    # Optional extraction query (if provided, uses existing "scraper" agent to extract relevant portions)
    query: str = ""

    # Execution controls
    wait_until: str = "domcontentloaded"  # Playwright goto waitUntil
    timeout_ms: int = 30000
    post_wait_ms: int = 0
    max_chars: int = 50000

    # Testing / orchestration validation
    dry_run: bool = False


class playwright_browse_arguments(BaseModel):
    tool_name: str
    arguments: playwright_browse_args

