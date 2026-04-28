# MCP in EmiAi (directory layout)

This folder is the home for **MCP server metadata** and (later) generated runtime caches.

## Structure

- `mcp/registry_sources/`
  - Snapshots and notes about upstream registries (e.g., official MCP Registry).
  - These are metadata-only and are safe to commit if they contain no secrets.

- `mcp/servers/`
  - EmiAi’s **curated allowlist** of MCP servers we choose to enable/support.
  - One file per server, with a stable `server_id` and a clear transport + launch/connect config.

- `mcp/tool_cache/` (generated; gitignored)
  - Runtime cache of `tools/list` results per server (tool names, descriptions, JSON schemas, etc.).
  - Treat as ephemeral; safe to delete and regenerate.

## Validation (recommended)

To validate all curated server entries:

```bash
python -m mcp.validate_servers
```

## Refreshing tool cache (optional)

To populate `mcp/tool_cache/` for an enabled server (stdio only for now):

```bash
python -m mcp.refresh_tool_cache --server-id io.modelcontextprotocol/time --launch-id pip
```

