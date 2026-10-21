"""Soma — адаптер MCP.

Тот же протокол, другой перевозчик. Конверт не меняется: MCP-клиент видит
инструменты, которые кладут и забирают конверты из очереди хаба.

    python3 -m soma.mcp_adapter            # stdio (по умолчанию)
    python3 -m soma.mcp_adapter --http 8765

Требуется mcp SDK (`pip install "soma-protocol[mcp]"`). Без него ядро работает
и без MCP — это осознанно: протокол не должен зависеть от перевозчика.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    print("нужен mcp SDK: pip install \"soma-protocol[mcp]\"", file=sys.stderr)
    raise

from .envelope import Envelope, Mandate, verify
from .memory import Memory
from .registry import Registry
from .server import Hub

mcp = MCPServer("soma", version="1.0.0")
_HUB: Hub | None = None


def hub() -> Hub:
    global _HUB
    if _HUB is None:
        _HUB = Hub(Memory(), Registry())
    return _HUB


def _envelope(**kwargs) -> str:
    mand = kwargs.pop("mandate", None)
    env = Envelope(**kwargs)
    if mand:
        env.mandate = Mandate(**{k: v for k, v in mand.items()
                                 if k in Mandate.__dataclass_fields__})
    return json.dumps(hub().send(env), ensure_ascii=False)


#: Публичный узел не хранит ничего приватного: видимость задаёт сервер.
#: Читается в момент вызова, а не при импорте — иначе смена режима не подхватывается.
def _forced_visibility() -> str:
    return os.environ.get("SOMA_FORCE_VISIBILITY", "")


@mcp.tool(name="soma_send",
          description="Отправить конверт агенту: цель, задача, получатель, бюджет токенов, мандат и цена. Конверт подписывается и попадает в очередь.")
async def soma_send(goal: str, task: str, to: str = "*", sender: str = "mcp",
                     capability: list[str] | None = None, token_budget: int = 2000,
                     context_query: str = "", max_cost_usd: float | None = None,
                     price_usdc: float | None = None, cost_usdc: float | None = None,
                     ttl_s: float = 3600,
                     visibility: str = "private") -> str:
    mand = {"max_cost_usd": max_cost_usd} if max_cost_usd is not None else None
    vis = _forced_visibility() or visibility
    return _envelope(goal=goal, task=task, to=to, sender=sender,
                     capability=capability or [], token_budget=token_budget,
                     context_query=context_query, mandate=mand,
                     price_usdc=price_usdc, cost_usdc=cost_usdc,
                     ttl_s=ttl_s, visibility=vis)


@mcp.tool(name="soma_inbox",
          description="Что ждёт агента: конверты, проверка подписи и контекст, собранный под бюджет токенов.")
async def soma_inbox(agent: str, limit: int = 10) -> str:
    return json.dumps(hub().inbox(agent, limit), ensure_ascii=False, default=str)


@mcp.tool(name="soma_claim",
          description="Забрать конверт из очереди (эксклюзивно) и получить контекст под бюджет. Если у работы назначена цена — нужна квитанция.")
async def soma_claim(id: str, agent: str, payment: str = "") -> str:
    return json.dumps(hub().claim(id, agent, payment=payment),
                      ensure_ascii=False, default=str)


@mcp.tool(name="soma_complete",
          description="Закрыть конверт результатом работы.")
async def soma_complete(id: str, result: str, agent: str = "") -> str:
    return json.dumps(hub().complete(id, {"text": result}, agent or None), ensure_ascii=False)


@mcp.tool(name="soma_route",
          description="Подобрать исполнителя по capability, мандату и цене. Если кандидата нет — вернётся честная причина отказа.")
async def soma_route(id: str) -> str:
    return json.dumps(hub().route(id), ensure_ascii=False, indent=2)


@mcp.tool(name="soma_verify",
          description="Проверить подпись конверта офлайн: Ed25519, публичный ключ едет вместе с подписью.")
async def soma_verify(envelope_json: str) -> str:
    env = Envelope.from_dict(json.loads(envelope_json))
    ok, why = verify(env)
    return json.dumps({"ok": ok, "reason": why, "algorithm": (env.signature or {}).get("algorithm")},
                      ensure_ascii=False)


@mcp.tool(name="soma_stats",
          description="Состояние хаба: конверты по статусам, факты, связи, холодные артефакты.")
async def soma_stats() -> str:
    return json.dumps(hub().memory.stats(), ensure_ascii=False, indent=2)



_PRIVACY = '# Privacy — soma\n\nsoma is a protocol for obligations between agents: who promised what, to whom,\nby when. That sentence is the privacy policy.\n\n## What is stored\n\n- **Envelopes** — the promise itself: sender, recipient, what was promised, the\n  deadline, the status. This is the product: an obligation you cannot store\n  cannot be kept.\n- **Request counters** — how many calls per tool per day.\n- **The rate limiter** keeps your client address in memory for one hour. It is\n  not written to disk.\n\n## What is not stored\n\n- your prompts, your model output, your files;\n- anything about your machine;\n- IP addresses in persistent form.\n\n## Who can read an envelope\n\nThe envelope exists so that the other side can act on it. It is readable by the\nsender, the recipient and the node that routes it. Do not put secrets in an\nenvelope: it is a record of an obligation, not a vault.\n\n## Deletion and questions\n\nOpen an issue at https://github.com/mrpkk/soma-protocol/issues. Envelopes are\nremoved on request; counters are not personal and are kept as totals.\n'

_LLMS_INSTALL = '# soma — install\n\nRemote MCP server (streamable HTTP). No key, no account.\n\n```json\n{"mcpServers": {"soma": {"url": "https://rta.lat/soma/mcp"}}}\n```\n\nTools: `soma_send` — отправить поручение, `soma_inbox` — входящие, `soma_claim` —\nвзять в работу, `soma_complete` — закрыть с доказательством, `soma_route` —\nмаршрут конверта, `soma_verify` — проверить выполнение, `soma_stats` — состояние узла.\n\nEndpoint: `https://rta.lat/soma/mcp` · Privacy: `https://rta.lat/soma/privacy` ·\nSource: https://github.com/mrpkk/soma-protocol\n'

def _build_app(http_path: str, rate_limit: int = 0, force_visibility: str = ""):
    """ASGI-приложение MCP: transport-security и управляемый профиль."""
    import hmac
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from .limits import IpRateLimit

    app = mcp.streamable_http_app(
        streamable_http_path=http_path,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
                           "rta.lat", "*.rta.lat"]))

    # /privacy и /llms-install.md: чек-лист каталогов MCP отвергает сервер,
    # у которого политика отдаёт 404 (см. attest/docs/MCP-CATALOG-CHECKLIST.md).
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    prefix = http_path.rsplit("/", 1)[0]

    async def _privacy(request):
        return PlainTextResponse(_PRIVACY, media_type="text/plain; charset=utf-8")

    async def _llms(request):
        return PlainTextResponse(_LLMS_INSTALL, media_type="text/plain; charset=utf-8")

    async def _health(request):
        return JSONResponse({"ok": True, "service": "soma", "mcp_path": http_path})

    for _p in ("/health", prefix + "/health"):
        app.router.routes.insert(0, Route(_p, _health))
    for _p in ("/privacy", prefix + "/privacy"):
        app.router.routes.insert(0, Route(_p, _privacy))
    for _p in ("/llms-install.md", prefix + "/llms-install.md"):
        app.router.routes.insert(0, Route(_p, _llms))

    # Управляемый профиль: обязательный bearer-токен.
    # Токен не настроен — значит вход закрыт (ошибка конфигурации не открывает дверь).
    token = os.environ.get("SOMA_TOKEN", "")
    if os.environ.get("SOMA_MANAGED") == "1":
        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                got = request.headers.get("authorization") or ""
                if not token or not hmac.compare_digest(got.strip(), f"Bearer {token}"):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerAuth)

    if rate_limit > 0:
        app.add_middleware(IpRateLimit(rate_limit).as_middleware())
    if force_visibility:
        # Видимость читается инструментом в момент вызова — выставляем здесь,
        # чтобы параметр сборки приложения и поведение инструмента совпадали.
        os.environ["SOMA_FORCE_VISIBILITY"] = force_visibility
    return app


def _run_http(args) -> None:
    import uvicorn
    uvicorn.run(_build_app(args.http_path, args.rate_limit_per_hour, args.force_visibility),
                host=args.host, port=args.http, log_level="warning")


def _run_stdio() -> None:
    asyncio.run(mcp.run_stdio_async())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0, help="порт streamable-http (0 = stdio)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8193") or 8193))
    ap.add_argument("--http-path", default="/hub/mcp",
                    help="путь MCP под префиксом туннеля (путь не срезается)")
    ap.add_argument("--rate-limit-per-hour", type=int,
                    default=int(os.environ.get("SOMA_RATE_LIMIT_PER_HOUR", "0") or 0),
                    help="лимит запросов в час на один IP (0 — без ограничения)")
    ap.add_argument("--force-visibility", default=os.environ.get("SOMA_FORCE_VISIBILITY", ""),
                    help="принудительная видимость для публичного узла (public)")
    args = ap.parse_args()
    if args.http:
        _run_http(args)
    else:
        _run_stdio()


if __name__ == "__main__":
    main()
