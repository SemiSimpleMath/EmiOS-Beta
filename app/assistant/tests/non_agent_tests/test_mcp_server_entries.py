from pathlib import Path


def test_mcp_server_entries_are_valid():
    # Lightweight regression test: ensure curated MCP entries conform to schema.
    from mcp.validate_servers import validate_all_server_entries

    repo_root = Path(__file__).resolve().parents[3]
    servers_dir = repo_root / "mcp" / "servers"

    issues = validate_all_server_entries(servers_dir=servers_dir)
    assert issues == [], "Invalid MCP server entries:\n" + "\n".join(
        f"- {i.file}: {i.message}" for i in issues
    )

