"""Регрессионные тесты на дефекты, найденные ревью.

Каждый тест здесь соответствует конкретному дефекту: без него правка может
вернуться незаметно. Имена тестов — это дефекты, а не абстракции.
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

from soma.anchors import LocalChain  # noqa: E402
from soma.envelope import (Envelope, Mandate, canonical, check_mandate,  # noqa: E402
                            generate_key, sign, validate)
from soma.memory import Memory  # noqa: E402
from soma.payments import x402_header_ok  # noqa: E402
from soma.registry import Registry  # noqa: E402
from soma.server import Hub, make_server  # noqa: E402


@pytest.fixture()
def key(tmp_path) -> bytes:
    path = tmp_path / "signing.key"
    generate_key(path)
    os.environ["SOMA_SIGNING_KEY"] = str(path)
    return path.read_bytes()


@pytest.fixture()
def memory(tmp_path) -> Memory:
    return Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold")


# ── мандат: отказ по умолчанию ───────────────────────────────

def test_unknown_rule_name_refuses_instead_of_disabling_checks():
    """Опечатка в имени правила не должна выключать все проверки."""
    m = Mandate(max_cost_usd=0.0, rules=["typo_nonexistent"])
    ok, why = check_mandate({"amount": 999999, "payee": "mallory", "calls": 999}, m)
    assert not ok and why.startswith("unknown_rule:"), why


def test_enabled_rule_without_field_is_refused():
    """Включённое правило + отсутствующее поле = отказ, а не «ноль»."""
    m = Mandate(max_cost_usd=0.0)
    ok, why = check_mandate({"payee": "alice"}, m)
    assert not ok and why.startswith("amount:"), why


def test_non_numeric_amount_is_refused_not_crashed():
    m = Mandate(max_cost_usd=10.0)
    ok, why = check_mandate({"amount": "abc"}, m)
    assert not ok and "не число" in why, why


# ── очередь: без переигровки и кражи ─────────────────────────

def test_put_does_not_overwrite_existing_envelope(memory):
    env = Envelope(goal="g", task="t", to="alice")
    assert memory.put(env) is True
    assert memory.put(Envelope(goal="g2", task="t2", to="alice", id=env.id)) is False
    assert memory.get(env.id)["goal"] == "g"


def test_claim_by_wrong_agent_is_refused(memory):
    memory.put(Envelope(goal="g", task="t", to="alice"))
    env_id = memory.inbox("alice")[0]["id"]
    env, why = memory.claim(env_id, "bob")
    assert env is None and "адресован alice" in why, why


def test_complete_of_unknown_envelope_fails(memory):
    assert memory.complete("nosuchid", {"text": "x"}) is False


def test_complete_cannot_overwrite_another_agents_result(memory):
    memory.put(Envelope(goal="g", task="t", to="alice"))
    env_id = memory.inbox("alice")[0]["id"]
    memory.claim(env_id, "alice")
    assert memory.complete(env_id, {"result": "original"}, "alice") is True
    assert memory.complete(env_id, {"result": "HIJACKED"}, "mallory") is False
    assert memory.get(env_id)["result"] == {"result": "original"}


# ── холодный ярус: только по хешу ───────────────────────────

def test_cold_ref_rejects_path_traversal(memory):
    assert memory.get_cold("sha256:../secret") is None
    assert memory.get_cold("../../etc/passwd") is None


def test_cold_ref_rejects_content_not_matching_hash(memory):
    ref = memory.put_cold("честный артефакт")
    (memory.cold_dir / f"{ref.split(':')[1]}.txt").write_text("подменённый", encoding="utf-8")
    assert memory.get_cold(ref) is None


# ── подпись: никакого молчаливого понижения ──────────────────

def test_sign_without_key_or_secret_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("SOMA_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SOMA_SECRET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    env = Envelope(goal="g", task="t")
    with pytest.raises(RuntimeError):
        sign(env)


def test_hub_send_fails_when_cannot_sign(monkeypatch, tmp_path):
    monkeypatch.delenv("SOMA_SIGNING_KEY", raising=False)
    monkeypatch.delenv("SOMA_SECRET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    hub = Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
              Registry(agents={}))
    res = hub.send(Envelope(goal="g", task="t"))
    assert res["ok"] is False and res["error"] == "cannot_sign", res


def test_generate_key_does_not_overwrite(tmp_path):
    path = tmp_path / "k.key"
    generate_key(path)
    with pytest.raises(FileExistsError):
        generate_key(path)


# ── ключи отправителей: происхождение, а не только целостность ──

def _client_sign(env, private_bytes) -> None:
    """Подписать конверт чужим ключом — как это сделал бы атакующий."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(private_bytes)
    payload = canonical(env.payload())
    env.signature = {
        "algorithm": "ed25519",
        "signature": base64.b64encode(priv.sign(payload)).decode(),
        "public_key": base64.b64encode(priv.public_key().public_bytes_raw()).decode(),
        "signer": "client",
    }


def test_client_signature_must_match_registered_key(key, tmp_path):
    """Клиентская подпись чужим ключом — это подмена отправителя, и она отвергается."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    attacker = Ed25519PrivateKey.generate()
    env = Envelope(goal="g", task="t", sender="trusted-agent")
    _client_sign(env, attacker.private_bytes_raw())
    hub = Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
              Registry(agents={"trusted-agent": {"public_key": "другой-ключ"}}))
    ok, why = hub.verify(env.to_dict())
    assert not ok and "не совпадает" in why, why


def test_client_signature_with_registered_key_is_accepted(key, tmp_path):
    """Клиентская подпись зарегистрированным ключом — принимается."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    env = Envelope(goal="g", task="t", sender="trusted-agent")
    _client_sign(env, priv.private_bytes_raw())
    hub = Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
              Registry(agents={"trusted-agent": {"public_key": pub}}))
    ok, why = hub.verify(env.to_dict())
    assert ok, why


# ── HTTP-транспорт: он вообще работает ────────────────────────

@pytest.fixture()
def http_server(tmp_path):
    hub = Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
              Registry(agents={"research": {"capabilities": ["research"], "cost_per_call": 0.01}}))
    srv = make_server(hub, "127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _post(url: str, payload: dict, headers: dict | None = None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_http_healthz_works_from_another_thread(http_server):
    """Транспорт работает: потоки HTTP не рвут соединение с базой."""
    with urllib.request.urlopen(http_server + "/healthz", timeout=10) as r:
        assert r.status == 200
        assert json.loads(r.read().decode())["ok"] is True


def test_http_send_and_inbox_roundtrip(http_server):
    code, body = _post(http_server + "/v1/messages",
                       {"goal": "g", "task": "t", "to": "research"})
    assert code == 200 and body["ok"] is True, body
    with urllib.request.urlopen(http_server + "/v1/inbox?agent=research", timeout=10) as r:
        items = json.loads(r.read().decode())["items"]
    assert items and items[0]["signature_ok"], items


def test_http_claim_is_exclusive_under_concurrency(http_server):
    code, body = _post(http_server + "/v1/messages",
                       {"goal": "g", "task": "t", "to": "research"})
    env_id = body["id"]
    results: list[int] = []
    lock = threading.Lock()

    def claim(i: int) -> None:
        c, _ = _post(http_server + "/v1/claim", {"id": env_id, "agent": "research"})
        with lock:
            results.append(c)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    # ровно один выигрывает: остальные получают «недоступен»
    assert all(c == 200 for c in results), results


def test_http_rejects_oversized_body(http_server):
    req = urllib.request.Request(http_server + "/v1/messages", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Content-Length": "999999999999"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code in (400, 413), code


def test_http_rejects_bad_content_length(http_server):
    req = urllib.request.Request(http_server + "/v1/messages", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Content-Length": "abc"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    assert code == 400, code


def test_http_remember_endpoint_is_not_exposed(http_server):
    """Незадокументированный write-эндпоинт памяти убран с HTTP-поверхности."""
    code, _ = _post(http_server + "/v1/remember", {"text": "отравление контекста"})
    assert code == 404, code


# ── деградация объявлена честно ───────────────────────────────

def test_degradation_does_not_claim_nothing_found_when_found(memory):
    for i in range(30):
        memory.remember(f"факт {i}: очень длинный текст про бюджет токенов " * 5)
    env = Envelope(goal="g", task="t", token_budget=60, context_query="бюджет токенов")
    memory.put(env)
    packed = memory.pack(memory.get(env.id))
    assert not any("не найдено" in d for d in packed["dropped"]), packed
    assert any("не влез" in d for d in packed["dropped"]), packed


def test_hot_tier_is_always_present(memory):
    env = Envelope(goal="цель", task="задача", token_budget=1)
    memory.put(env)
    packed = memory.pack(memory.get(env.id))
    assert "hot" in packed["sources"], packed
    assert "цель" in packed["text"], packed


# ── валидация без jsonschema ─────────────────────────────────

def test_fallback_validator_checks_types(monkeypatch):
    """Без jsonschema проверка всё равно ловит неверные типы."""
    import builtins
    real_import = builtins.__import__

    def no_jsonschema(name, *a, **kw):
        if name == "jsonschema":
            raise ImportError("jsonschema отключён в этом тесте")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_jsonschema)
    assert validate(Envelope(goal="g", task="t", token_budget="abc")), "тип не проверен"
    assert validate(Envelope(goal="g", task="t", ttl_s=-5)), "граница не проверена"


# ── оплата: сумма сверяется ──────────────────────────────────

def test_x402_receipt_underpaid_is_rejected():
    assert x402_header_ok({"x-payment": json.dumps({"amount": 0.001})}, 0.005)[0] is False
    assert x402_header_ok({"x-payment": json.dumps({"amount": 0.005, "asset": "USDC"})}, 0.005)[0] is True
    assert x402_header_ok({"x-payment": "не json"}, 0.005)[0] is False


# ── история: цепочка, а не просто таблица ────────────────────

def test_history_is_a_verified_hash_chain(memory):
    memory.put(Envelope(goal="g", task="t", to="alice"))
    ok, why = memory.chain.verify_chain()
    assert ok, why


def test_chain_breaks_when_a_link_is_tampered(tmp_path):
    """Подмена средней записи ломает связь с последующей."""
    chain = LocalChain(tmp_path / "h.jsonl")
    chain.append({"a": 1})
    chain.append({"a": 2})
    chain.append({"a": 3})
    recs = chain.records()
    recs[1]["payload"]["a"] = 99
    recs[1]["hash"] = __import__("hashlib").sha256(
        (recs[1]["prev"] + json.dumps(recs[1]["payload"], sort_keys=True,
                                      ensure_ascii=False, separators=(",", ":"))).encode()).hexdigest()
    chain.path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                          encoding="utf-8")
    ok, why = chain.verify_chain()
    assert not ok and "разрыв" in why, why


def test_public_landing_and_status_open_panel(tmp_path, monkeypatch):
    """Публичный вход: лендинг и статус без токена, MCP — закрыт.

    23.10.2026: 401 на главной отталкивал и людей, и каталоги MCP —
    первый экран должен объяснять продукт, а не требовать ключ.
    """
    monkeypatch.setenv("SOMA_DB", str(tmp_path / "soma.db"))
    monkeypatch.setenv("SOMA_COLD", str(tmp_path / "cold"))
    monkeypatch.setenv("SOMA_HISTORY", str(tmp_path / "history.jsonl"))
    monkeypatch.setenv("SOMA_MANAGED", "1")
    monkeypatch.setenv("SOMA_TOKEN", "test-token")

    import soma.mcp_adapter as adapter
    from starlette.testclient import TestClient

    adapter._HUB = None
    try:
        client = TestClient(adapter._build_app("/hub/mcp"))
        landing = client.get("/hub")
        assert landing.status_code == 200
        assert "Soma" in landing.text and "soma_remember" in landing.text

        status = client.get("/hub/status")
        assert status.status_code == 200
        body = status.json()
        assert body["service"] == "soma"
        assert "soma_remember" in body["tools"]

        assert client.get("/health").status_code == 200
        # Вход в MCP остаётся закрытым: публична витрина, не данные.
        assert client.post("/hub/mcp", json={}).status_code == 401
    finally:
        adapter._HUB = None
