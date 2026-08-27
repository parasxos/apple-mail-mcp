# Introspection-only container: the server functionally requires macOS with
# Mail.app, but it starts anywhere and serves the full MCP tool catalog,
# which is what directory health checks (e.g. Glama) exercise.
FROM python:3.12-slim
RUN pip install --no-cache-dir apple-mailbox-mcp
ENTRYPOINT ["apple-mailbox-mcp"]
