# MCP server entries (`mcp/servers/`)

This directory contains EmiAi’s **curated MCP server allowlist**.

Each file is a single server entry (YAML) validated against the canonical schema:

- `mcp/schemas/mcp_server_entry.schema.json`

## Naming convention

- Prefer one folder per namespace, e.g. `mcp/servers/io.modelcontextprotocol/`.
- File name is the server “short name”, e.g. `time.yaml`.
- Full stable identifier is stored in `server_id` (example: `io.modelcontextprotocol/time`).

## Tool namespacing (important)

Tool names will collide at scale. Treat each MCP tool as namespaced:

- `mcp::<server_id>::<tool_name>`
  - Example: `mcp::io.modelcontextprotocol/time::get_current_time`

## Security note

These files are **metadata**, not trust anchors.

- Do **not** embed secrets in `env` or `headers`.
- Use `policy.*` as the host-enforced default safety posture.

