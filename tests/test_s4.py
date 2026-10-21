"""S4: серверный профиль — аутентификация, квоты, лимиты.

Локальный режим (по умолчанию) — для работы за своим шлюзом.
Управляемый профиль (`SOMA_MANAGED=1`) — то, что можно выставлять наружу.
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

from soma.envelope import Envelope, generate_key  # noqa: E402
from soma.server import Hub, make_server  # noqa: E402


@pytest.fixture()
def key(tmp_path, monkeypatch):
    path = tmp_path / "signing.key"
    generate_key(path)
    monkeypatch.setenv("SOMA_SIGNING_KEY", str(path))
    return path


def _serve(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    hub = Hub()
    srv = make_server(hub, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return f"http://127.0.0.1:{port}"


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


# ── локальный режим ──────────────────────────────────────────

def test_local_mode_needs_no_token(monkeypatch, key):
    base = _serve(monkeypatch, SOMA_MANAGED="0")
    code, body = _post(base + "/v1/messages", {"goal": "g", "task": "t"})
    assert code == 200 and body["ok"], body


# ── управляемый режим ────────────────────────────────────────

def test_managed_mode_requires_token(monkeypatch, key):
    base = _serve(monkeypatch, SOMA_MANAGED="1", SOMA_TOKEN="secret-token")
    code, body = _post(base + "/v1/messages", {"goal": "g", "task": "t"})
    assert code == 401 and body["error"] == "unauthorized", body
    code, body = _get(base + "/healthz")
    assert code == 401, body


def test_managed_mode_accepts_correct_token(monkeypatch, key):
    base = _serve(monkeypatch, SOMA_MANAGED="1", SOMA_TOKEN="secret-token")
    code, body = _post(base + "/v1/messages", {"goal": "g", "task": "t"},
                       {"Authorization": "Bearer secret-token"})
    assert code == 200 and body["ok"], body


def test_managed_mode_without_configured_token_denies_everything(monkeypatch, key):
    """Токен не настроен — значит, вход закрыт, а не открыт."""
    base = _serve(monkeypatch, SOMA_MANAGED="1")
    monkeypatch.delenv("SOMA_TOKEN", raising=False)
    code, _ = _post(base + "/v1/messages", {"goal": "g", "task": "t"},
                    {"Authorization": "Bearer secret-token"})
    assert code == 401, code


# ── квоты ────────────────────────────────────────────────────

def test_quota_limits_sends(monkeypatch, key):
    base = _serve(monkeypatch, SOMA_QUOTA_PER_HOUR="2")
    ok = 0
    limited = 0
    for _ in range(4):
        code, body = _post(base + "/v1/messages", {"goal": "g", "task": "t", "sender": "noisy"})
        if code == 200 and body.get("ok"):
            ok += 1
        elif code == 429:
            limited += 1
    assert ok == 2, f"прошло {ok}, ожидалось 2"
    assert limited == 2, f"отклонено {limited}, ожидалось 2"


def test_quota_is_per_sender(monkeypatch, key):
    base = _serve(monkeypatch, SOMA_QUOTA_PER_HOUR="1")
    code, _ = _post(base + "/v1/messages", {"goal": "g", "task": "t", "sender": "a"})
    assert code == 200
    code, _ = _post(base + "/v1/messages", {"goal": "g", "task": "t", "sender": "a"})
    assert code == 429
    code, _ = _post(base + "/v1/messages", {"goal": "g", "task": "t", "sender": "b"})
    assert code == 200, "чужой отправитель не должен исчерпывать чужую квоту"


# ── лимит тела ───────────────────────────────────────────────

def test_body_limit_is_configurable(monkeypatch, key):
    base = _serve(monkeypatch, SOMA_MAX_BODY="2048")
    big = {"goal": "g", "task": "t" * 5000}
    code, body = _post(base + "/v1/messages", big)
    assert code == 400, (code, body)
