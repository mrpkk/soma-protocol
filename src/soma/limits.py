"""Soma — ограничение по IP на транспортном уровне.

Публичный узел принимает конверты от кого угодно. Значит, нужен предел:
сколько запросов один клиент может сделать в час. Считаем **на транспорте**,
до разбора тела, — иначе злоупотребление успевает нагрузить парсер.

Счётчик в памяти процесса: при перезапуске обнуляется, и это осознанно —
это защита от случайного залпа, а не биллинг.
"""

from __future__ import annotations

import time
from typing import Any


class IpRateLimit:
    """Лимит запросов на IP в час. Нулевой лимит = без ограничения."""

    def __init__(self, per_hour: int = 0):
        self.per_hour = max(0, int(per_hour))
        self.counts: dict[tuple[str, int], int] = {}

    def client_key(self, scope: str, request: Any) -> str:
        """Ключ клиента.

        За Cloudflare-туннелем сокет всегда 127.0.0.1, поэтому без заголовка с
        реальным адресом **весь мир делит один счётчик**: 30 запросов в час на
        всех, и один каталог-сканер выбивает лимит у остальных.

        `CF-Connecting-IP` добавляет сам Cloudflare (его подставлять нельзя —
        это единственный заголовок, который клиент не контролирует). Дальше —
        `X-Forwarded-For` (его задаёт прокси перед нами), и лишь потом адрес
        сокета.
        """
        for header in ("cf-connecting-ip", "x-forwarded-for"):
            value = ""
            try:
                value = request.headers.get(header, "") or ""
            except Exception:
                value = ""
            if value:
                return value.split(",")[0].strip()
        try:
            return request.client.host or "unknown"
        except Exception:
            return scope

    def check(self, scope: str, request: Any) -> tuple[bool, int]:
        """Разрешить или отказать. Возвращает (ok, сколько осталось)."""
        if self.per_hour <= 0:
            return True, -1
        key = self.client_key(scope, request)
        window = int(time.time() // 3600)
        slot = (key, window)
        used = self.counts.get(slot, 0)
        if used >= self.per_hour:
            return False, 0
        self.counts[slot] = used + 1
        # чистим прошлые окна, чтобы счётчик не рос бесконечно
        if len(self.counts) > 4096:
            for k in [k for k in self.counts if k[1] < window - 1]:
                self.counts.pop(k, None)
        return True, self.per_hour - used - 1

    def as_middleware(self):
        """Ставится на ASGI-приложение: отказ — 429 с понятным телом."""
        from starlette.responses import JSONResponse

        limit = self

        class RateLimitMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    await self.app(scope, receive, send)
                    return
                ok, left = limit.check(scope.get("path", "/"), _Request(scope, receive))
                if not ok:
                    response = JSONResponse(
                        {"error": "rate_limited",
                         "details": [f"лимит {limit.per_hour} запросов в час на клиента"]},
                        status_code=429)
                    await response(scope, receive, send)
                    return
                await self.app(scope, receive, send)

        return RateLimitMiddleware


class _Request:
    """Минимальная обёртка над ASGI-сообщением: нужны только заголовки и клиент."""

    def __init__(self, scope: dict, receive: Any):
        self.scope = scope
        self._receive = receive
        self._headers: dict[str, str] = {}
        self._loaded = False

    @property
    def headers(self) -> dict[str, str]:
        if not self._loaded:
            self._loaded = True
            for k, v in self.scope.get("headers") or []:
                self._headers[k.decode("latin-1").lower()] = v.decode("latin-1")
        return self._headers

    @property
    def client(self):
        host, port = (self.scope.get("client") or (None, None))
        return type("C", (), {"host": host, "port": port})()
