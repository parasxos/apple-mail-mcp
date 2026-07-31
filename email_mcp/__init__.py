"""email-mcp: local MCP server exposing read-only access to Apple Mail."""
# THE version of this project — the single source of truth. pyproject.toml
# declares `version` dynamic and reads this attribute at build time (static
# AST parse, no import), so distribution metadata cannot drift from what an
# import reports. Bumping a release means editing this line and nothing else.
__version__ = "0.11.0"
