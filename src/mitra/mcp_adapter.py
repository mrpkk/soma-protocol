"""Mitra — адаптер MCP.

Тот же протокол, другой перевозчик. Конверт не меняется: MCP-клиент видит
инструменты, которые кладут и забирают конверты из очереди хаба.

    python3 -m mitra.mcp_adapter            # stdio (по умолчанию)
    python3 -m mitra.mcp_adapter --http 8765

Требуется mcp SDK (`pip install "mitra-hub[mcp]"`). Без него ядро работает
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
    print("нужен mcp SDK: pip install \"mitra-hub[mcp]\"", file=sys.stderr)
    raise

from .envelope import Envelope, Mandate, verify
from .memory import Memory
from .registry import Registry
from .server import Hub

mcp = MCPServer("mitra", version="1.0.0")
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


@mcp.tool(name="mitra_send",
          description="Отправить конверт агенту: цель, задача, получатель, бюджет токенов, мандат и цена. Конверт подписывается и попадает в очередь.")
async def mitra_send(goal: str, task: str, to: str = "*", sender: str = "mcp",
                     capability: list[str] | None = None, token_budget: int = 2000,
                     context_query: str = "", max_cost_usd: float | None = None,
                     price_usdc: float | None = None, ttl_s: float = 3600) -> str:
    mand = {"max_cost_usd": max_cost_usd} if max_cost_usd is not None else None
    return _envelope(goal=goal, task=task, to=to, sender=sender,
                     capability=capability or [], token_budget=token_budget,
                     context_query=context_query, mandate=mand,
                     price_usdc=price_usdc, ttl_s=ttl_s)


@mcp.tool(name="mitra_inbox",
          description="Что ждёт агента: конверты, проверка подписи и контекст, собранный под бюджет токенов.")
async def mitra_inbox(agent: str, limit: int = 10) -> str:
    return json.dumps(hub().inbox(agent, limit), ensure_ascii=False, default=str)


@mcp.tool(name="mitra_claim",
          description="Забрать конверт из очереди (эксклюзивно) и получить контекст под бюджет.")
async def mitra_claim(id: str, agent: str) -> str:
    return json.dumps(hub().claim(id, agent), ensure_ascii=False, default=str)


@mcp.tool(name="mitra_complete",
          description="Закрыть конверт результатом работы.")
async def mitra_complete(id: str, result: str, agent: str = "") -> str:
    return json.dumps(hub().complete(id, {"text": result}, agent or None), ensure_ascii=False)


@mcp.tool(name="mitra_route",
          description="Подобрать исполнителя по capability, мандату и цене. Если кандидата нет — вернётся честная причина отказа.")
async def mitra_route(id: str) -> str:
    return json.dumps(hub().route(id), ensure_ascii=False, indent=2)


@mcp.tool(name="mitra_verify",
          description="Проверить подпись конверта офлайн: Ed25519, публичный ключ едет вместе с подписью.")
async def mitra_verify(envelope_json: str) -> str:
    env = Envelope.from_dict(json.loads(envelope_json))
    ok, why = verify(env)
    return json.dumps({"ok": ok, "reason": why, "algorithm": (env.signature or {}).get("algorithm")},
                      ensure_ascii=False)


@mcp.tool(name="mitra_stats",
          description="Состояние хаба: конверты по статусам, факты, связи, холодные артефакты.")
async def mitra_stats() -> str:
    return json.dumps(hub().memory.stats(), ensure_ascii=False, indent=2)


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0, help="порт streamable-http (0 = stdio)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--http-path", default="/mcp")
    args = ap.parse_args()
    if args.http:
        import uvicorn
        from mcp.server.transport_security import TransportSecuritySettings
        app = mcp.streamable_http_app(
            streamable_http_path=args.http_path,
            transport_security=TransportSecuritySettings(
                allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"]))
        uvicorn.run(app, host=args.host, port=args.http, log_level="warning")
    else:
        await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(_main())
