# Soma MCP server — reproducible run for Glama checks and local use.
# stdio transport: the client speaks JSON-RPC over stdin/stdout.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

# No token needed for a local node; set SOMA_MANAGED=1 + SOMA_TOKEN to require one.
ENTRYPOINT ["python3", "-m", "soma.mcp_adapter"]
