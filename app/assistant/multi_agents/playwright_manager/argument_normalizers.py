def normalize_playwright_tool_args(result_dict: dict, pending_tool: dict, blackboard) -> dict:
    selected_tool = pending_tool.get("name") if isinstance(pending_tool, dict) else None

    # `browser_wait_for.time` is in SECONDS, but agents sometimes provide milliseconds.
    if (
        isinstance(result_dict, dict)
        and selected_tool == "mcp::npm/playwright-mcp::browser_wait_for"
        and isinstance(result_dict.get("time"), (int, float))
    ):
        t = result_dict.get("time")
        # Only apply when it strongly looks like ms (>=1000 and divisible by 1000).
        if isinstance(t, (int, float)) and t >= 1000 and float(t).is_integer() and int(t) % 1000 == 0:
            result_dict["time"] = int(t) // 1000

    # Force "press Enter after typing" to avoid stalling after text entry.
    # (`browser_type` supports `submit: true` which sends Enter.)
    if isinstance(result_dict, dict) and selected_tool == "mcp::npm/playwright-mcp::browser_type":
        # Some schemas return args directly, others wrap as {"arguments": {...}}.
        if isinstance(result_dict.get("arguments"), dict):
            args = result_dict["arguments"]
            # Only set if it looks like a browser_type payload.
            if "ref" in args and "text" in args and args.get("submit") is not True:
                args["submit"] = True
        else:
            if "ref" in result_dict and "text" in result_dict and result_dict.get("submit") is not True:
                result_dict["submit"] = True

    return result_dict
