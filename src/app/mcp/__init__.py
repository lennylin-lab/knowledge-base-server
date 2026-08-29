"""MCP extension mechanism: external server connections and tool access.

Client-only (this repo never implements MCP servers). The layer imports
`core/` alone — it is pure infrastructure and must stay free of `api/`,
`services/`, `agents/`, and `models/` (directory-structure.md).
"""
