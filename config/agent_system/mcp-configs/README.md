# MCP Configs

This folder holds MCP server templates.

- Set `GITHUB_TOKEN` before enabling the GitHub server.
- Set `ARXIV_STORAGE_PATH` for the arXiv server (defaults to ~/.arxiv-mcp-server/papers).
- Set `CLICKHOUSE_*` env vars for the ClickHouse server.
- Ensure `uv`/`uvx` are installed for ClickHouse and Git servers.
- Docker MCP requires a running Docker Engine (WSL integration or local daemon).
- Filesystem MCP uses CLI args for allowed paths (`${PWD}`).
