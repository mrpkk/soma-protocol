# Soma MCP server — reproducible run for Glama checks and local use.
# stdio transport: the client speaks JSON-RPC over stdin/stdout.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[mcp]"

# No token needed for a local node; set SOMA_MANAGED=1 + SOMA_TOKEN to require one.
#
# Одна команда на два окружения: здесь пакет стоит в систему и `soma-mcp` лежит в
# PATH, а каталоги (Glama) собирают образ через `uv sync`, где скрипт живёт только
# в .venv. Без этой развилки их сборка падает с ModuleNotFoundError.
ENTRYPOINT ["sh", "-c", "if command -v soma-mcp >/dev/null 2>&1; then exec soma-mcp; else exec uv run soma-mcp; fi"]
