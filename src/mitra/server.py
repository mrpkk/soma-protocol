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

from .envelope import (Envelope, check_mandate, route as route_envelope, sign, verify)
from .memory import Memory
from .registry import Registry


class Hub:
    """Ядро хаба: память + реестр. Транспорт — снаружи."""

    def __init__(self, memory: Memory | None = None, registry: Registry | None = None):
        self.memory = memory or Memory()
        self.registry = registry or Registry()

    # ── операции ──────────────────────────────────────────────
    def send(self, env: Envelope, sign_it: bool = True,
             client_signature: dict[str, Any] | None = None) -> dict[str, Any]:
        """Принять конверт.

        Клиентская подпись принимается: тогда происхождение проверяется по
        реестру. Если подписи нет — конверт подписывает хаб, и происхождение
        отвечает хаб, а не отправителю.
        """
        errors = _validate(env)
        if errors:
            return {"ok": False, "error": "invalid_envelope", "details": errors}
        if client_signature:
            env.signature = dict(client_signature)
            env.signature.setdefault("signer", "client")
            ok, why = self.verify(env.to_dict())
            if not ok:
                return {"ok": False, "error": "bad_signature", "details": [why]}
        elif sign_it:
            try:
                sign(env)
            except RuntimeError as exc:
                return {"ok": False, "error": "cannot_sign", "details": [str(exc)]}
        if not self.memory.put(env):
            return {"ok": False, "error": "duplicate_id", "details": ["конверт с таким id уже есть"]}
        return {"ok": True, "id": env.id, "signature": (env.signature or {}).get("algorithm", "none")}

    def verify(self, envelope: dict[str, Any]) -> tuple[bool, str]:
        """Проверить подпись конверта, сверив отправителя с реестром ключей."""
        env = Envelope.from_dict(envelope)
        expected = self.registry.public_key(env.sender)
        return verify(env, expected_public_key=expected)

    def stats(self) -> dict[str, Any]:
        return self.memory.stats()

    def inbox(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for env in self.memory.inbox(agent, limit):
            ok, why = verify(Envelope.from_dict(env))
            out.append({"envelope": env, "signature_ok": ok, "signature": why,
                        "context": self.memory.pack(env)})
        return out

    def claim(self, env_id: str, agent: str) -> dict[str, Any]:
        """Забрать конверт.

        Проверки идут ДО захвата: отказ не должен сжигать конверт —
        легитимный исполнитель должен получить его потом.
        """
        env = self.memory.get(env_id)
        if not env:
            return {"ok": False, "reason": "нет такого конверта"}
        if env.get("status") != "queued":
            return {"ok": False, "reason": f"конверт недоступен (status={env.get('status')})"}
        if env.get("to") not in (agent, "*"):
            return {"ok": False, "reason": f"конверт адресован {env.get('to')}"}
        ok, sig_why = self.verify(env)
        if not ok:
            return {"ok": False, "reason": f"подпись не прошла: {sig_why}"}
        mand = env.get("mandate") or None
        if mand:
            from .envelope import Mandate
            m = Mandate(**{k: v for k, v in mand.items()
                           if k in Mandate.__dataclass_fields__})
            allowed, rule_why = check_mandate({"payee": env.get("to"), "calls": 1}, m,
                                              only=["payee", "calls", "validity_window"])
            if not allowed:
                return {"ok": False, "reason": f"мандат отклонил выдачу: {rule_why}"}
        claimed, why = self.memory.claim(env_id, agent)
        if claimed is None:
            return {"ok": False, "reason": why}
        return {"ok": True, "envelope": claimed, "context": self.memory.pack(claimed)}

    def complete(self, env_id: str, result: dict[str, Any], agent: str | None = None) -> dict[str, Any]:
        """Закрыть конверт результатом. Фактическая стоимость сверяется с мандатом."""
        env = self.memory.get(env_id)
        if env and isinstance(result, dict) and "cost_usd" in result:
            mand = env.get("mandate") or None
            if mand:
                from .envelope import Mandate
                m = Mandate(**{k: v for k, v in mand.items()
                               if k in Mandate.__dataclass_fields__})
                allowed, why = check_mandate({"amount": result["cost_usd"]}, m, only=["amount"])
                if not allowed:
                    return {"ok": False, "id": env_id, "reason": f"мандат отклонил результат: {why}"}
        ok = self.memory.complete(env_id, result, agent)
        return {"ok": ok, "id": env_id, "reason": "ok" if ok else "конверт не найден или занят другим"}

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

    MAX_BODY = 1_048_576  # 1 МиБ: конверт — это письмо, а не файл

    def _body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len or 0)
        except (TypeError, ValueError):
            raise ValueError("неверный Content-Length")
        if length < 0 or length > self.MAX_BODY:
            raise ValueError(f"размер тела вне границ (максимум {self.MAX_BODY})")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise ValueError("тело не является JSON")

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
            try:
                limit = int((q.get("limit") or ["10"])[0])
            except ValueError:
                limit = 10
            limit = max(1, min(limit, 100))
            self._send(200, {"agent": agent, "items": self.hub.inbox(agent, limit)})
        elif u.path.startswith("/v1/envelopes/"):
            env_id = u.path.rsplit("/", 1)[-1]
            env = self.hub.memory.get(env_id)
            if not env:
                self._send(404, {"error": "not_found"})
                return
            ok, why = self.hub.verify(env)
            self._send(200, {"envelope": env, "signature_ok": ok, "signature": why,
                             "history": self.hub.memory.history(env_id)})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        try:
            body = self._body()
        except ValueError as exc:
            self._send(400, {"error": "bad_request", "details": [str(exc)]})
            return
        if u.path == "/v1/messages":
            env = Envelope.from_dict({k: v for k, v in body.items()
                                      if k in Envelope.__dataclass_fields__})
            client_sig = body.get("signature") if isinstance(body.get("signature"), dict) else None
            self._send(200, self.hub.send(env, sign_it=True, client_signature=client_sig))
        elif u.path == "/v1/claim":
            self._send(200, self.hub.claim(body.get("id", ""), body.get("agent", "")))
        elif u.path == "/v1/complete":
            self._send(200, self.hub.complete(body.get("id", ""), body.get("result") or {},
                                               body.get("agent")))
        elif u.path == "/v1/route":
            self._send(200, self.hub.route(body.get("id", "")))
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
