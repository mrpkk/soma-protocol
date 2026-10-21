"""S8: публичная точка входа — лимит по IP и принудительная видимость.

Публичный узел принимает конверты от кого угодно, поэтому два ограничения
обязательны и должны быть покрыты тестами:

1. **Лимит по IP** на транспорте: клиент не может съесть узел (429).
2. **Видимость задаёт сервер**: публичный узел не хранит ничего приватного,
   даже если отправитель попросил `private`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from soma.limits import IpRateLimit  # noqa: E402


class _FakeRequest:
    def __init__(self, ip: str = "10.0.0.1", forwarded: str = ""):
        self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
        self.client = type("C", (), {"host": ip, "port": 0})()


def test_rate_limit_counts_per_ip():
    limit = IpRateLimit(per_hour=3)
    for _ in range(3):
        ok, _ = limit.check("/x", _FakeRequest(ip="10.0.0.1"))
        assert ok
    ok, left = limit.check("/x", _FakeRequest(ip="10.0.0.1"))
    assert not ok and left == 0


def test_rate_limit_is_per_client():
    """Чужой клиент не расходует чужую квоту."""
    limit = IpRateLimit(per_hour=1)
    assert limit.check("/x", _FakeRequest(ip="10.0.0.1"))[0] is True
    assert limit.check("/x", _FakeRequest(ip="10.0.0.1"))[0] is False
    assert limit.check("/x", _FakeRequest(ip="10.0.0.2"))[0] is True


def test_forwarded_for_wins_over_socket_ip():
    """За туннелем реальный IP клиента — в X-Forwarded-For."""
    limit = IpRateLimit(per_hour=1)
    assert limit.check("/x", _FakeRequest(ip="10.0.0.1", forwarded="203.0.113.9"))[0]
    assert not limit.check("/x", _FakeRequest(ip="10.0.0.1", forwarded="203.0.113.9"))[0]


def test_zero_limit_means_unlimited():
    limit = IpRateLimit(per_hour=0)
    for _ in range(100):
        assert limit.check("/x", _FakeRequest())[0]


# ── публичный узел целиком ───────────────────────────────────

@pytest.fixture()
def public_node(tmp_path, monkeypatch):
    """Публичный узел: отдельные данные, видимость public, лимит 5/час."""
    monkeypatch.setenv("SOMA_DB", str(tmp_path / "public.db"))
    monkeypatch.setenv("SOMA_COLD", str(tmp_path / "cold"))
    monkeypatch.setenv("SOMA_REPUTATION", str(tmp_path / "rep.json"))
    monkeypatch.setenv("SOMA_AGENTS", str(tmp_path / "agents.json"))
    (tmp_path / "agents.json").write_text(json.dumps(
        {"researcher": {"capabilities": ["research"], "cost_per_call": 0.0}}), encoding="utf-8")
    from soma.mcp_adapter import _build_app
    import uvicorn
    app = _build_app("/soma/mcp", rate_limit=5, force_visibility="public")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/soma/mcp", tmp_path
    server.should_exit = True
    time.sleep(0.3)


def _rpc(url, method, params, mid, session):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session["id"]:
        headers["mcp-session-id"] = session["id"]
    payload = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                session["id"] = sid
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read()[:120].decode("utf-8", "replace")}
    for line in raw.splitlines():
        if line.startswith("data:"):
            return 200, json.loads(line[5:].strip())
    return 200, json.loads(raw) if raw.strip() else {}


def test_public_node_keeps_nothing_private(public_node):
    url, tmp_path = public_node
    session = {"id": None}
    _rpc(url, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}, 1, session)
    code, res = _rpc(url, "tools/call",
                     {"name": "soma_send",
                      "arguments": {"goal": "приватная", "task": "t", "to": "researcher",
                                   "visibility": "private"}}, 2, session)
    assert code == 200, res
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "public.db"))
    row = conn.execute("SELECT visibility FROM envelopes ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row[0] == "public", f"публичный узел сохранил непубличное: {row[0]}"


def test_public_node_refuses_after_the_quota(public_node):
    url, _ = public_node
    session = {"id": None}
    _rpc(url, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}, 1, session)
    codes = []
    for i in range(9):
        code, _ = _rpc(url, "tools/list", {}, 10 + i, session)
        codes.append(code)
    assert 429 in codes, f"квота не сработала: {codes}"


def test_path_alias_serves_the_same_endpoint():
    """Второй публичный путь переписывается на канонический — без второго процесса."""
    import asyncio
    from soma.limits import PathAlias

    seen = {}

    async def app(scope, receive, send):
        seen["path"] = scope["path"]

    mw = PathAlias("/soma/mcp", "/soma-protocol/mcp").as_middleware()(app)
    asyncio.run(mw({"type": "http", "path": "/soma-protocol/mcp", "headers": []}, None, None))
    assert seen["path"] == "/soma/mcp"
    asyncio.run(mw({"type": "http", "path": "/soma/mcp", "headers": []}, None, None))
    assert seen["path"] == "/soma/mcp"
