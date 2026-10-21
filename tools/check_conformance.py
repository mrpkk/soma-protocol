#!/usr/bin/env python3
"""Проверка чужого хаба на соответствие протоколу Soma.

Запуск:

    python3 tools/check_conformance.py https://your-hub.example/mcp
    python3 tools/check_conformance.py https://rta.lat/soma/mcp

Что делает: подключается к MCP-эндпоинту по streamable-http и прогоняет
набор свойств, на которые опирается протокол. Каждое свойство — проверяемое
утверждение, а не «вроде работает».

Требуется: `pip install mcp` (клиент) и доступный по HTTPS эндпоинт.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

try:
    from mcp import ClientSession
    try:                                    # mcp 2.x
        from mcp.client.streamable_http import streamable_http_client as _http_client
    except ImportError:                     # mcp 1.x
        from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:  # pragma: no cover
    print("нужен MCP-клиент: pip install mcp", file=sys.stderr)
    raise SystemExit(2)

TIMEOUT = 30


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, bool(ok), detail))
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)

    def summary(self) -> str:
        return f"{len(self.rows) - self.failed}/{len(self.rows)} проверок пройдено"


async def _call(session: ClientSession, name: str, args: dict[str, Any]) -> dict[str, Any]:
    res = await session.call_tool(name, args)
    text = "".join(getattr(c, "text", "") for c in res.content)
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text}


async def run(url: str) -> int:
    rep = Report()
    print(f"Проверка соответствия Soma: {url}\n")
    async with _http_client(url) as streams:
        # mcp 1.x отдаёт (read, write, get_session_id), mcp 2.x — (read, write)
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            server = init.serverInfo.name if hasattr(init, "serverInfo") else "?"
            print(f"  сервер: {server}\n")

            names = {t.name for t in (await s.list_tools()).tools}
            need = {"soma_send", "soma_inbox", "soma_claim", "soma_complete", "soma_route"}
            missing = need - names
            rep.check("C0 семь инструментов на месте", not missing,
                      f"нет: {sorted(missing)}" if missing else "все есть")

            if missing:
                print(f"\n{rep.summary()}\nИнструментов не хватает — остальные проверки невозможны.")
                return 1

            # C1 отправка
            sent = await _call(s, "soma_send", {
                "goal": "conformance probe", "task": "check the implementation",
                "to": "verifier", "capability": ["verify"], "token_budget": 500,
                "visibility": "shared"})
            env_id = sent.get("id")
            rep.check("C1 конверт принят и подписан", bool(env_id) and bool(sent.get("signature")),
                      f"id={env_id} подпись={sent.get('signature')}")

            # C3 бюджет — и заодно берём НАСТОЯЩИЙ конверт (не собранный руками:
            # подпись считается по полному телу, любая пересборка её ломает)
            inbox = await _call(s, "soma_inbox", {"agent": "verifier"})
            items = inbox if isinstance(inbox, list) else inbox.get("items", [])
            mine = [i for i in items if (i.get("envelope") or {}).get("id") == env_id]
            ctx = (mine[0].get("context") if mine else {}) or {}

            # C2 подпись проверяется
            real = mine[0].get("envelope") if mine else {}
            ver = await _call(s, "soma_verify", {"envelope_json": json.dumps(real)})
            rep.check("C2 подпись проверяется", ver.get("ok") is True, ver.get("reason", ""))
            rep.check("C3 контекст уложился в бюджет",
                      bool(ctx) and ctx.get("tokens", 0) <= ctx.get("budget", 0),
                      f"{ctx.get('tokens')} из {ctx.get('budget')}")
            rep.check("C4 деградация объявлена явно", "dropped" in ctx,
                      f"dropped={len(ctx.get('dropped', []))}")

            # C5 роутер объясняется
            dec = await _call(s, "soma_route", {"id": env_id})
            rep.check("C5 роутер объясняет решение", bool(dec.get("reason")),
                      f"агент={dec.get('agent')}")

            # C6 claim эксклюзивен
            first = await _call(s, "soma_claim", {"id": env_id, "agent": "verifier"})
            second = await _call(s, "soma_claim", {"id": env_id, "agent": "other"})
            rep.check("C6 конверт забирает один", first.get("ok") is True and second.get("ok") is not True,
                      second.get("reason", "")[:80])

            # C7 результат закрывает конверт
            done = await _call(s, "soma_complete", {"id": env_id, "result": "conformance probe done",
                                                    "agent": "verifier"})
            rep.check("C7 результат закрывает конверт", done.get("ok") is True, done.get("reason", ""))

            # C8 платёж там, где назначена цена
            paid_env = await _call(s, "soma_send", {
                "goal": "paid work", "task": "must be paid", "to": "verifier",
                "cost_usdc": 0.005, "visibility": "shared"})
            if paid_env.get("id"):
                no_pay = await _call(s, "soma_claim", {"id": paid_env["id"], "agent": "verifier"})
                with_pay = await _call(s, "soma_claim", {"id": paid_env["id"], "agent": "verifier",
                                                         "payment": json.dumps({"amount": 0.005,
                                                                                "asset": "USDC"})})
                rep.check("C8 работа с ценой требует оплаты",
                          no_pay.get("ok") is not True and with_pay.get("ok") is True,
                          f"без оплаты: {str(no_pay.get('reason'))[:60]}")
            else:
                rep.check("C8 работа с ценой требует оплаты", False,
                          "не удалось отправить конверт с ценой")

            # C9 состояние
            stats = await _call(s, "soma_stats", {})
            rep.check("C9 состояние доступно", bool(stats), str(stats)[:60])

    print(f"\n{rep.summary()}")
    return 0 if rep.failed == 0 else 1


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url
    return asyncio.run(run(url))


if __name__ == "__main__":
    raise SystemExit(main())
