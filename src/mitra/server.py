"""Mitra — транспорт.

Одно письмо — много перевозчиков. Ядро не знает, кто доставит конверт:
- HTTP/JSON — свой, на стандартной библиотеке, без фреймворков;
- MCP — адаптер (если установлен mcp SDK): те же семь инструментов;
- A2A, x402, WS/SSE — точки подключения, описанные в PROTOCOL.md.

HTTP-эндпоинты:
    GET  /healthz              живо ли сердце
    GET  /v1/agents            реестр исполнителей
    POST /v1/messages          отправить конверт
    GET  /v1/inbox?agent=      что ждёт агента
    POST /v1/claim             забрать конверт (атомарно)
    POST /v1/complete          закрыть конверт результатом
    POST /v1/route             подобрать исполнителя
    GET  /v1/envelopes/{id}    конверт + подпись + история
"""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .envelope import Envelope, route as route_envelope, sign, verify
from .memory import Memory
from .registry import Registry


class Hub:
    """Ядро хаба: память + реестр. Транспорт — снаружи."""

    def __init__(self, memory: Memory | None = None, registry: Registry | None = None):
        self.memory = memory or Memory()
        self.registry = registry or Registry()

    # ── операции ──────────────────────────────────────────────
    def send(self, env: Envelope, sign_it: bool = True) -> dict[str, Any]:
        errors = _validate(env)
        if errors:
            return {"ok": False, "error": "invalid_envelope", "details": errors}
        if sign_it:
            sign(env)
        self.memory.put(env)
        return {"ok": True, "id": env.id, "signature": (env.signature or {}).get("algorithm", "none")}

    def inbox(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for env in self.memory.inbox(agent, limit):
            ok, why = verify(Envelope.from_dict(env))
            out.append({"envelope": env, "signature_ok": ok, "signature": why,
                        "context": self.memory.pack(env)})
        return out

    def claim(self, env_id: str, agent: str) -> dict[str, Any]:
        env, why = self.memory.claim(env_id, agent)
        if env is None:
            return {"ok": False, "reason": why}
        return {"ok": True, "envelope": env, "context": self.memory.pack(env)}

    def complete(self, env_id: str, result: dict[str, Any], agent: str | None = None) -> dict[str, Any]:
        ok = self.memory.complete(env_id, result, agent)
        return {"ok": ok, "id": env_id}

    def route(self, env_id: str) -> dict[str, Any]:
        env = self.memory.get(env_id)
        if not env:
            return {"ok": False, "reason": "нет такого конверта"}
        decision = route_envelope(Envelope.from_dict(env), self.registry.agents)
        self.memory.set_routing(env_id, decision)
        return decision


def _validate(env: Envelope) -> list[str]:
    from .envelope import validate
    return validate(env)


class Handler(BaseHTTPRequestHandler):
    hub: Hub  # проставляется в make_server

    server_version = "mitra/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в лог
        if os.environ.get("MITRA_QUIET") == "1":
            return
        super().log_message(fmt, *args)

    # ── ответы ────────────────────────────────────────────────
    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    # ── маршруты ──────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/healthz":
            self._send(200, {"ok": True, "protocol": "mitra/1", "time": time.time(),
                             "stats": self.hub.memory.stats()})
        elif u.path == "/v1/agents":
            self._send(200, {"agents": self.hub.registry.agents})
        elif u.path == "/v1/inbox":
            agent = (q.get("agent") or [""])[0]
            limit = int((q.get("limit") or ["10"])[0])
            self._send(200, {"agent": agent, "items": self.hub.inbox(agent, limit)})
        elif u.path.startswith("/v1/envelopes/"):
            env_id = u.path.rsplit("/", 1)[-1]
            env = self.hub.memory.get(env_id)
            if not env:
                self._send(404, {"error": "not_found"})
                return
            ok, why = verify(Envelope.from_dict(env))
            self._send(200, {"envelope": env, "signature_ok": ok, "signature": why,
                             "history": self.hub.memory.history(env_id)})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/v1/messages":
            env = Envelope.from_dict({k: v for k, v in body.items()
                                      if k in Envelope.__dataclass_fields__})
            self._send(200, self.hub.send(env, sign_it=body.get("sign", True)))
        elif u.path == "/v1/claim":
            self._send(200, self.hub.claim(body.get("id", ""), body.get("agent", "")))
        elif u.path == "/v1/complete":
            self._send(200, self.hub.complete(body.get("id", ""), body.get("result") or {},
                                               body.get("agent")))
        elif u.path == "/v1/route":
            self._send(200, self.hub.route(body.get("id", "")))
        elif u.path == "/v1/remember":
            fid = self.hub.memory.remember(body.get("text", ""), tags=body.get("tags", ""),
                                            importance=int(body.get("importance", 3)),
                                            source=body.get("source", ""))
            self._send(200, {"ok": True, "id": fid})
        else:
            self._send(404, {"error": "not_found"})


def make_server(hub: Hub | None = None, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"hub": hub or Hub()})
    return ThreadingHTTPServer((host, port), handler)


def serve(host: str = "127.0.0.1", port: int = 8765, hub: Hub | None = None) -> None:
    srv = make_server(hub, host, port)
    print(f"mitra: слушаю http://{host}:{port}  (POST /v1/messages)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
