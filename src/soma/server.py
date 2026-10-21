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

import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .components import Components, build_components
from .federation import Peer, export_history, import_history, push as federate_push  # noqa: E402
from .reputation import ReputationStore
from .envelope import Envelope, Mandate, route as route_envelope
from .memory import Memory
from .registry import Registry


class Hub:
    """Ядро хаба: компоненты (память, реестр, подпись, мандат, учёт, якорь).

    Транспорт — снаружи. Любой компонент заменяется плагином; в OSS лежат
    эталонные реализации, настоящие движки (Mitra Core) подключаются сверху.
    """

    def __init__(self, memory: Memory | None = None, registry: Registry | None = None,
                 components: Components | None = None,
                 reputation: ReputationStore | None = None):
        self.components = components or build_components(memory, registry)
        self.memory = self.components.memory
        self.registry = self.components.registry
        self.reputation = reputation or ReputationStore()

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
                self.components.signer.sign(env)
            except RuntimeError as exc:
                return {"ok": False, "error": "cannot_sign", "details": [str(exc)]}
        if not self.memory.put(env):
            return {"ok": False, "error": "duplicate_id", "details": ["конверт с таким id уже есть"]}
        return {"ok": True, "id": env.id, "signature": (env.signature or {}).get("algorithm", "none")}

    def verify(self, envelope: dict[str, Any]) -> tuple[bool, str]:
        """Проверить подпись конверта, сверив отправителя с реестром ключей."""
        env = Envelope.from_dict(envelope)
        expected = self.registry.public_key(env.sender)
        return self.components.signer.verify(env, expected_public_key=expected)

    def stats(self) -> dict[str, Any]:
        return self.memory.stats()

    # ── федерация ───────────────────────────────────────────
    def federate_push(self, env_id: str, peers: list[dict[str, Any]]) -> dict[str, Any]:
        """Разослать конверт партнёрам. Приватный — не уйдёт никуда."""
        env = self.memory.get(env_id)
        if not env:
            return {"ok": False, "reason": "нет такого конверта"}
        peer_objs = [Peer(name=p.get("name", "peer"), url=p.get("url", ""),
                           public_key=p.get("public_key")) for p in peers if p.get("url")]
        return federate_push(Envelope.from_dict(env), peer_objs, self.components.signer)

    def federate_receive(self, payload: dict[str, Any], from_peer: str = "") -> dict[str, Any]:
        """Принять конверт от узла-партнёра: подпись проверяется по нашему реестру."""
        from .federation import verify_from_peer
        sender = (payload.get("envelope") or {}).get("sender") or from_peer
        expected = self.registry.public_key(sender) if sender else None
        ok, why = verify_from_peer(payload.get("envelope") or {}, expected)
        if not ok:
            return {"ok": False, "reason": why}
        env = Envelope.from_dict(payload["envelope"])
        if (env.visibility or "private") == "private":
            return {"ok": False, "reason": "конверт приватный — принять нельзя"}
        stored = self.memory.put(env)
        return {"ok": stored, "id": env.id, "from": sender,
                "reason": "ok" if stored else "конверт с таким id уже есть"}

    def federate_outbox(self) -> dict[str, Any]:
        """Что мы готовы отдать другим узлам."""
        return {"envelopes": self.memory.outbox()}

    def history_export(self) -> dict[str, Any]:
        return export_history(self.memory.chain)

    def history_import(self, exported: dict[str, Any]) -> dict[str, Any]:
        """Принять чужую цепочку истории и проверить её непрерывность."""
        ok, why = import_history(exported, self.memory.chain)
        return {"ok": ok, "reason": why}

    def inbox(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for env in self.memory.inbox(agent, limit):
            ok, why = self.verify(env)
            out.append({"envelope": env, "signature_ok": ok, "signature": why,
                        "context": self.memory.pack(env)})
        return out

    def claim(self, env_id: str, agent: str,
              payment: str | dict | None = None) -> dict[str, Any]:
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
        # Оплата: если работа что-то стоит — нужна квитанция, иначе отказ.
        cost = env.get("cost_usdc")
        if cost:
            from .payments import check_receipt
            paid, pay_why = check_receipt(payment, float(cost))
            if not paid:
                return {"ok": False, "reason": f"требуется оплата {cost} USDC: {pay_why}",
                        "code": "payment_required", "amount_usdc": float(cost)}
        # Оценщик зовётся ВСЕГДА, даже если в конверте нет мандата:
        # движок с собственными правилами (Mitra Core) обязан уметь отказать
        # и без явного мандата. Эталон без мандата просто пропускает.
        mand = env.get("mandate") or None
        m = Mandate(**{k: v for k, v in (mand or {}).items()
                       if k in Mandate.__dataclass_fields__}) if mand else None
        allowed, rule_why = self.components.mandate.check(
            {"payee": env.get("to"), "calls": 1}, m,
            only=["payee", "calls", "validity_window"])
        if not allowed:
            self.reputation.record_breach(agent, env_id, rule_why.split(":")[0],
                                          {"rule": rule_why})
            return {"ok": False, "reason": f"мандат отклонил выдачу: {rule_why}"}
        claimed, why = self.memory.claim(env_id, agent)
        if claimed is None:
            return {"ok": False, "reason": why}
        return {"ok": True, "envelope": claimed, "context": self.memory.pack(claimed)}

    def complete(self, env_id: str, result: dict[str, Any], agent: str | None = None,
                 receipt: dict[str, Any] | None = None) -> dict[str, Any]:
        """Закрыть конверт результатом. Фактическая стоимость сверяется с мандатом."""
        env = self.memory.get(env_id)
        if env and isinstance(result, dict) and "cost_usd" in result:
            mand = env.get("mandate") or None
            m = Mandate(**{k: v for k, v in (mand or {}).items()
                           if k in Mandate.__dataclass_fields__}) if mand else None
            allowed, why = self.components.mandate.check(
                {"amount": result.get("cost_usd", 0.0)}, m, only=["amount"])
            if not allowed:
                return {"ok": False, "id": env_id, "reason": f"мандат отклонил результат: {why}"}
        ok = self.memory.complete(env_id, result, agent)
        if ok and receipt and agent:
            expected = self.registry.public_key(agent)
            good, why = self.reputation.record_receipt(receipt, expected)
            if not good:
                return {"ok": True, "id": env_id, "reason": "ok",
                        "receipt": "не засчитана: " + why,
                        "reputation": self.reputation.score(agent)}
        return {"ok": ok, "id": env_id, "reason": "ok" if ok else "конверт не найден или занят другим",
                "reputation": self.reputation.score(agent) if agent else None}

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
    """HTTP-поверхность протокола.

    По умолчанию — локальный режим: без аутентификации, для работы за своим
    шлюзом. Профиль `SOMA_MANAGED=1` включает обязательный bearer-токен и
    квоты: это то, что нужно для публикации наружу.
    """

    hub: Hub  # проставляется в make_server
    server_version = "soma/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # тише в лог
        if os.environ.get("SOMA_QUIET") == "1":
            return
        super().log_message(fmt, *args)

    # ── серверный профиль ───────────────────────────────────
    @property
    def managed(self) -> bool:
        return os.environ.get("SOMA_MANAGED") == "1"

    def _auth_ok(self) -> bool:
        """Bearer-токен, если включён управляемый профиль."""
        if not self.managed:
            return True
        expected = os.environ.get("SOMA_TOKEN", "")
        if not expected:
            return False
        got = self.headers.get("authorization") or ""
        return hmac.compare_digest(got.strip(), f"Bearer {expected}")

    def _deny(self) -> None:
        self._send(401, {"error": "unauthorized",
                         "details": ["нужен заголовок Authorization: Bearer <SOMA_TOKEN>"]})

    def _quota_exceeded(self, who: str) -> bool:
        """Квота на отправителя/получателя. Возвращает True, если лимит выбран."""
        limit = int(os.environ.get("SOMA_QUOTA_PER_HOUR", "0") or 0)
        if limit <= 0:
            return False
        now = time.time()
        window = int(now // 3600)
        counts = self.server.quota  # type: ignore[attr-defined]
        key = f"{who}:{window}"
        if counts.get(key, 0) >= limit:
            return True
        counts[key] = counts.get(key, 0) + 1
        return False

    # ── ответы ────────────────────────────────────────────────
    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    DEFAULT_MAX_BODY = 1_048_576  # 1 МиБ: конверт — это письмо, а не файл

    @property
    def max_body(self) -> int:
        """Лимит тела читается на каждый запрос: конфиг может измениться."""
        try:
            return int(os.environ.get("SOMA_MAX_BODY", str(self.DEFAULT_MAX_BODY)))
        except ValueError:
            return self.DEFAULT_MAX_BODY

    def _body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len or 0)
        except (TypeError, ValueError):
            raise ValueError("неверный Content-Length")
        limit = self.max_body
        if length < 0 or length > limit:
            raise ValueError(f"размер тела вне границ (максимум {limit})")
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            raise ValueError("тело не является JSON")

    # ── маршруты ──────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._deny()
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/.well-known/agent-card.json", "/.well-known/agent.json", "/agents.json"):
            from .a2a import agent_card
            self._send(200, agent_card(
                name=os.environ.get("SOMA_AGENT_NAME", "soma"),
                url=os.environ.get("SOMA_AGENT_URL", f"http://{self.headers.get('Host', '127.0.0.1')}/mcp"),
                description=os.environ.get("SOMA_AGENT_DESCRIPTION",
                                           "Soma — протокол коммуникации агентов: конверт, бюджет, мандат, подпись."),
                skills=["send", "claim", "complete", "route", "verify"]))
        elif u.path == "/healthz":
            self._send(200, {"ok": True, "protocol": "soma/1", "time": time.time(),
                             "components": self.hub.components.describe(),
                             "stats": self.hub.memory.stats()})
        elif u.path == "/v1/agents":
            self._send(200, {"agents": self.hub.registry.agents})
        elif u.path == "/v1/agents/nonce":
            agent = (q.get("agent") or [""])[0]
            if not agent:
                self._send(400, {"error": "bad_request", "details": ["нужен параметр agent"]})
                return
            self._send(200, {"agent": agent, "nonce": self.hub.registry.issue_nonce(agent)})
        elif u.path == "/v1/federate/outbox":
            self._send(200, self.hub.federate_outbox())
        elif u.path == "/v1/federate/pull":
            peer = (q.get("peer") or [""])[0]
            from .federation import pull
            code, body = pull(peer)
            self._send(200, {"peer": peer, "code": code, "body": body})
        elif u.path == "/v1/history":
            self._send(200, self.hub.history_export())
        elif u.path == "/v1/reputation":
            agent = (q.get("agent") or [""])[0]
            if agent:
                self._send(200, self.hub.reputation.score(agent))
            else:
                self._send(200, {"agents": {a: self.hub.reputation.score(a)
                                           for a in self.hub.reputation.agents()}})
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
        if not self._auth_ok():
            self._deny()
            return
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
            if self._quota_exceeded(env.sender or "unknown"):
                self._send(429, {"error": "quota_exceeded",
                                 "details": [f"лимит отправки исчерпан"]})
                return
            self._send(200, self.hub.send(env, sign_it=True, client_signature=client_sig))
        elif u.path == "/v1/claim":
            payment = body.get("payment") or self.headers.get("x-payment")
            self._send(200, self.hub.claim(body.get("id", ""), body.get("agent", ""), payment))
        elif u.path == "/v1/complete":
            receipt = body.get("receipt") if isinstance(body.get("receipt"), dict) else None
            self._send(200, self.hub.complete(body.get("id", ""), body.get("result") or {},
                                               body.get("agent"), receipt))
        elif u.path == "/v1/federate/push":
            self._send(200, self.hub.federate_push(body.get("id", ""), body.get("peers") or []))
        elif u.path == "/v1/federate/inbox":
            self._send(200, self.hub.federate_receive(body, body.get("from", "")))
        elif u.path == "/v1/history":
            ok, why = import_history(body, self.hub.memory.chain)
            self._send(200, {"ok": ok, "reason": why})
        elif u.path == "/v1/agents":
            ok, why = self.hub.registry.register(
                agent=body.get("agent", ""), public_key=body.get("public_key", ""),
                capabilities=body.get("capabilities") or [],
                cost_per_call=float(body.get("cost_per_call", 0.0) or 0.0),
                proof=body.get("proof"), metadata=body.get("metadata"))
            self._send(200 if ok else 400, {"ok": ok, "reason": why})
        elif u.path == "/v1/route":
            self._send(200, self.hub.route(body.get("id", "")))
        else:
            self._send(404, {"error": "not_found"})


def make_server(hub: Hub | None = None, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"hub": hub or Hub()})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.quota = {}  # type: ignore[attr-defined]
    return srv


def serve(host: str = "127.0.0.1", port: int = 8765, hub: Hub | None = None) -> None:
    srv = make_server(hub, host, port)
    print(f"soma: слушаю http://{host}:{port}  (POST /v1/messages)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
