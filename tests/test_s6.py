"""S6: федерация — узлы видят друг друга, история переносима.

Два узла в одном процессе (разные базы и порты) — как два настоящих узла
в разных местах.
"""

from __future__ import annotations

import base64
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
from soma.federation import Peer, export_history, import_history, push, verify_from_peer  # noqa: E402
from soma.memory import Memory  # noqa: E402
from soma.registry import Registry  # noqa: E402
from soma.reputation import ReputationStore  # noqa: E402
from soma.server import Hub, make_server  # noqa: E402


def _keypair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    return (priv.private_bytes_raw(),
            base64.b64encode(priv.public_key().public_bytes_raw()).decode())


@pytest.fixture()
def key(tmp_path, monkeypatch):
    path = tmp_path / "signing.key"
    generate_key(path)
    monkeypatch.setenv("SOMA_SIGNING_KEY", str(path))
    return path


def _node(tmp_path, name: str) -> tuple[Hub, str]:
    """Узел: своя база, свой реестр, свой порт."""
    hub = Hub(memory=Memory(path=tmp_path / f"{name}.db", cold_dir=tmp_path / f"{name}-cold"),
              registry=Registry(path=tmp_path / f"{name}-agents.json", agents={}),
              reputation=ReputationStore(path=tmp_path / f"{name}-rep.json"))
    srv = make_server(hub, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return hub, f"http://127.0.0.1:{port}"


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def _get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return r.status, json.loads(r.read().decode())


# ── видимость ────────────────────────────────────────────────

def test_private_envelope_never_leaves_the_node(key, tmp_path):
    hub_a, url_a = _node(tmp_path, "a")
    env = Envelope(goal="g", task="t", to="worker")
    hub_a.send(env)
    peers = [Peer(name="b", url="http://127.0.0.1:1")]  # заведомо недоступный
    res = push(Envelope.from_dict(hub_a.memory.get(env.id)), peers, hub_a.components.signer)
    assert res["pushed"] is False
    assert "приватный" in res["reason"], res


def test_shared_envelope_is_pushed(key, tmp_path):
    hub_a, url_a = _node(tmp_path, "a")
    hub_b, url_b = _node(tmp_path, "b")
    env = Envelope(goal="g", task="t", to="worker", visibility="shared")
    hub_a.send(env)
    res = hub_a.federate_push(env.id, [{"name": "b", "url": url_b}])
    assert res["pushed"] is True, res
    assert res["peers"][0]["ok"] is True, res
    # узел B принял и может отдать конверт в работу
    assert hub_b.memory.get(env.id) is not None
    inbox = hub_b.inbox("worker")
    assert inbox, "конверт от партнёра должен быть виден получателю"


def test_peer_signature_is_verified_against_the_registry(key, tmp_path):
    hub_a, url_a = _node(tmp_path, "a")
    hub_b, url_b = _node(tmp_path, "b")
    private, public = _keypair()
    # узел B знает ключ узла A (зарегистрировал через KYA раньше)
    hub_b.registry.agents["node-a"] = {"capabilities": [], "cost_per_call": 0.0,
                                       "public_key": public}
    hub_b.registry.save()
    env = Envelope(goal="g", task="t", to="worker", visibility="shared", sender="node-a")
    hub_a.send(env)
    # подписываем от имени A его ключом
    from soma.envelope import canonical
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(private)
    env.signature = {"algorithm": "ed25519",
                     "signature": base64.b64encode(priv.sign(canonical(env.payload()))).decode(),
                     "public_key": public, "signer": "hub"}
    res = hub_b.federate_receive({"envelope": env.to_dict(), "from": "node-a"}, "node-a")
    assert res["ok"] is True, res


def test_foreign_key_is_refused(key, tmp_path):
    hub_b, url_b = _node(tmp_path, "b")
    private, public = _keypair()
    hub_b.registry.agents["node-a"] = {"capabilities": [], "cost_per_call": 0.0,
                                       "public_key": "другой-ключ"}
    hub_b.registry.save()
    env = Envelope(goal="g", task="t", to="worker", visibility="shared", sender="node-a")
    from soma.envelope import canonical
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(private)
    env.signature = {"algorithm": "ed25519",
                     "signature": base64.b64encode(priv.sign(canonical(env.payload()))).decode(),
                     "public_key": public, "signer": "hub"}
    res = hub_b.federate_receive({"envelope": env.to_dict(), "from": "node-a"}, "node-a")
    assert res["ok"] is False
    assert "не совпадает" in res["reason"], res


def test_private_envelope_from_peer_is_refused(key, tmp_path):
    hub_b, url_b = _node(tmp_path, "b")
    env = Envelope(goal="g", task="t", to="worker", visibility="private")
    env.signature = {"algorithm": "ed25519", "signature": "x", "public_key": "y", "signer": "hub"}
    res = hub_b.federate_receive({"envelope": env.to_dict()}, "node-a")
    assert res["ok"] is False, res


# ── история ──────────────────────────────────────────────────

def test_history_survives_the_death_of_a_node(key, tmp_path):
    """История одного узла принимается и проверяется другим."""
    hub_a, url_a = _node(tmp_path, "a")
    hub_b, url_b = _node(tmp_path, "b")
    env = Envelope(goal="g", task="t", to="worker")
    hub_a.send(env)
    hub_a.claim(env.id, "worker")
    exported = hub_a.history_export()
    assert exported["count"] >= 1
    ok, why = import_history(exported, hub_b.memory.chain)
    assert ok, why
    # цепочка B осталась целой после приёма
    ok2, why2 = hub_b.memory.chain.verify_chain()
    assert ok2, why2


def test_broken_history_is_refused(key, tmp_path):
    hub_a, url_a = _node(tmp_path, "a")
    hub_b, url_b = _node(tmp_path, "b")
    hub_a.send(Envelope(goal="g", task="t"))
    exported = hub_a.history_export()
    exported["records"][0]["prev"] = "0" * 63 + "1"   # разрыв
    ok, why = import_history(exported, hub_b.memory.chain)
    assert not ok, why


def test_history_is_served_over_http(key, tmp_path):
    hub_a, url_a = _node(tmp_path, "a")
    hub_a.send(Envelope(goal="g", task="t"))
    code, body = _get(url_a + "/v1/history")
    assert code == 200 and body["count"] >= 1, body
