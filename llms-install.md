# Installing apple-mail-mcp (for AI agents)

Requirements: macOS with Mail.app configured. Python is not required separately when using uv.

1. Ensure the host terminal has Full Disk Access (System Settings > Privacy & Security > Full Disk Access). This cannot be granted programmatically; ask the user to toggle it, then restart the terminal.
2. Install and run setup:
   uvx apple-mailbox-mcp setup
3. Register the server with the MCP client. Command: `uvx`, args: `["apple-mailbox-mcp"]`, transport: stdio.
   Claude Code: claude mcp add --transport stdio --scope user apple-mail -- uvx apple-mailbox-mcp
4. Verify: uvx apple-mailbox-mcp status
   All checks green means ready. The doctor tool inside the server reports the same with fixes per red line.

Notes for agents:
- The PyPI package name is apple-mailbox-mcp; the repo is apple-mail-mcp.
- First body-index build on a large mailbox runs in background; search works immediately.
- Read-only mode: set EMAIL_MCP_READ_ONLY=1 in the server env to disable all mutating tools.
