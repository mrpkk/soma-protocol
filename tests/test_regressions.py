"""Регрессии второго ревью: маршрутизация, отказы и конкуренция.

Эти тесты закрывают дефекты, которые появились ИЗ ПРАВОК: маршрутизация
ломала подпись, отказ при claim сжигал конверт, а база и цепочка истории
рассыпались под нагрузкой.
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
from soma.memory import Memory  # noqa: E402
from soma.registry import Registry  # noqa: E402
from soma.server import Hub, make_server  # noqa: E402


@pytest.fixture()
def key(tmp_path):
    path = tmp_path / "signing.key"
    generate_key(path)
    os.environ["SOMA_SIGNING_KEY"] = str(path)
    return path


@pytest.fixture()
def hub(tmp_path) -> Hub:
    return Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
               Registry(agents={"research": {"capabilities": ["research"], "cost_per_call": 0.01},
                                "coder": {"capabilities": ["code"], "cost_per_call": 0.02}}))


@pytest.fixture()
def server(tmp_path):
    srv = make_server(Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
                          Registry(agents={"research": {"capabilities": ["research"],
                                                        "cost_per_call": 0.01}})),
                      "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _post(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")
    except Exception as e:
        return 0, {"error": type(e).__name__}


# ── R1: маршрутизация не должна ломать подпись ───────────────

def test_route_keeps_the_signature_valid(hub):
    """route меняет назначение, а не подписанное поле `to`."""
    env = Envelope(goal="g", task="t", to="*", capability=["research"])
    assert hub.send(env)["ok"]
    decision = hub.route(env.id)
    assert decision["agent"] == "research", decision
    stored = hub.memory.get(env.id)
    assert stored["to"] == "*", "подписанное поле `to` менять нельзя"
    assert stored["assigned_to"] == "research", stored
    ok, why = hub.verify(stored)
    assert ok, f"подпись должна остаться верной после route: {why}"
    claim = hub.claim(env.id, "research")
    assert claim["ok"], claim


def test_route_then_claim_over_http(server):
    code, body = _post(server + "/v1/messages",
                       {"goal": "g", "task": "t", "to": "*", "capability": ["research"]})
    assert code == 200 and body["ok"], body
    env_id = body["id"]
    code, decision = _post(server + "/v1/route", {"id": env_id})
    assert code == 200 and decision.get("agent") == "research", decision
    code, claim = _post(server + "/v1/claim", {"id": env_id, "agent": "research"})
    assert code == 200 and claim["ok"], claim


# ── R3: отказ не должен сжигать конверт ──────────────────────

def test_refused_claim_leaves_envelope_queued(hub):
    """Мандат, отклонивший выдачу, не забирает конверт у легитимного исполнителя."""
    from soma.envelope import Mandate
    env = Envelope(goal="g", task="t", to="alice",
                   mandate=Mandate(allowed_payees=["bob"]))
    assert hub.send(env)["ok"]
    refused = hub.claim(env.id, "alice")
    assert refused["ok"] is False, refused
    assert hub.memory.get(env.id)["status"] == "queued", "отказ не должен сжигать конверт"
    # и легитимный получатель по-прежнему может его взять (после снятия мандата)
    assert hub.memory.inbox("alice"), "конверт должен остаться в очереди"


def test_claim_by_wrong_agent_leaves_envelope_queued(hub):
    env = Envelope(goal="g", task="t", to="alice")
    hub.send(env)
    refused = hub.claim(env.id, "mallory")
    assert refused["ok"] is False
    assert hub.memory.get(env.id)["status"] == "queued"


# ── R4/R5: конкуренция ───────────────────────────────────────

def test_concurrent_puts_lose_nothing(tmp_path):
    """Под конкуренцией ни один принятый конверт не теряется."""
    mem = Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold")
    ok_count = 0
    lock = threading.Lock()

    def put(i: int) -> None:
        nonlocal ok_count
        res = mem.put(Envelope(goal=f"g{i}", task="t", to="alice"))
        with lock:
            ok_count += 1 if res else 0

    threads = [threading.Thread(target=put, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stored = mem.conn.execute("SELECT COUNT(*) c FROM envelopes").fetchone()["c"]
    assert stored == ok_count == 100, f"принято {ok_count}, в базе {stored}"


def test_concurrent_http_load_does_not_drop_requests(server):
    """8 потоков × 25 запросов: ни одного оборванного соединения."""
    results: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        for j in range(25):
            code, _ = _post(server + "/v1/messages",
                            {"goal": f"g{i}-{j}", "task": "t", "to": "research"})
            with lock:
                results.append(code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 200
    bad = [c for c in results if c != 200]
    assert not bad, f"оборвано/ошибок: {len(bad)} из {len(results)}"


def test_concurrent_history_chain_stays_intact(tmp_path):
    """Цепочка истории не рвётся под конкуренцией."""
    mem = Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold")

    def work(i: int) -> None:
        for j in range(30):
            mem.event(f"env{i}-{j}", "put", {"i": i, "j": j})

    threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ok, why = mem.chain.verify_chain()
    assert ok, why


# ── R7: один и тот же вердикт подписи в разных местах ───────

def test_signature_verdict_is_the_same_everywhere(server):
    """inbox и карточка конверта дают один и тот же вердикт."""
    code, body = _post(server + "/v1/messages",
                       {"goal": "g", "task": "t", "to": "research"})
    env_id = body["id"]
    with urllib.request.urlopen(server + "/v1/inbox?agent=research", timeout=10) as r:
        inbox = json.loads(r.read().decode())
    with urllib.request.urlopen(server + f"/v1/envelopes/{env_id}", timeout=10) as r:
        detail = json.loads(r.read().decode())
    assert inbox["items"][0]["signature_ok"] == detail["signature_ok"], \
        "вердикт подписи должен совпадать везде"
    assert inbox["items"][0]["signature"] == detail["signature"]


# ── R2: клиентская подпись принимается хабом ─────────────────

def test_hub_accepts_client_signature_and_checks_registry(server):
    """Клиент подписывает сам — хаб сверяет ключ с реестром."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    env = Envelope(goal="g", task="t", to="research", sender="trusted")
    from soma.envelope import canonical
    payload = canonical(env.payload())
    sig = {"algorithm": "ed25519",
           "signature": base64.b64encode(priv.sign(payload)).decode(),
           "public_key": pub, "signer": "client"}
    code, body = _post(server + "/v1/messages", env.to_dict() | {"signature": sig})
    assert code == 200 and body["ok"], body


# ── R3: подпись не зависит от типа числа и переживает хранение ─────

def test_canonical_makes_numbers_type_stable():
    """3600 и 3600.0 — одно и то же тело (иначе JS и Python не сойдутся)."""
    from soma.envelope import canonical
    assert canonical({"ttl_s": 3600}) == canonical({"ttl_s": 3600.0})
    assert canonical({"x": [1, 2.0, 3.5]}) == canonical({"x": [1.0, 2, 3.5]})
    # bool — не число: True нельзя превращать в 1
    assert canonical({"x": True}) != canonical({"x": 1})


def test_signature_survives_int_ttl_through_storage(tmp_path):
    """Конверт с целым ttl_s читается из базы с float — подпись обязана сойтись.

    Регрессия: REAL-колонка возвращала 3600.0 там, где подписано 3600,
    и любой, кто проверял конверт ПОСЛЕ работы узла, получал InvalidSignature.
    """
    from soma.envelope import Envelope, sign, verify
    from soma.memory import Memory

    mem = Memory(str(tmp_path / "r3.db"), str(tmp_path / "cold"))
    env = Envelope(goal="g", task="t", to="verifier", sender="probe",
                   token_budget=500, ttl_s=3600, visibility="shared")
    sign(env)
    assert verify(env)[0]
    mem.put(env)
    stored = Envelope.from_dict(mem.get(env.id))
    ok, why = verify(stored)
    assert ok, f"подпись не пережила хранение: {why}"
