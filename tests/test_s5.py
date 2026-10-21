"""S5: реестр внешних агентов, KYA и репутация по фактам.

Проверяем главное: в реестр нельзя попасть с чужим ключом, репутация считается
только по проверяемым квитанциям, а нарушение по мандату не остаётся без следа.
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
from soma.reputation import (ReputationStore, receipt_payload, result_hash,  # noqa: E402
                             sign_receipt, verify_receipt)
from soma.registry import Registry  # noqa: E402
from soma.server import Hub, make_server  # noqa: E402


def _keypair() -> tuple[bytes, str]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes_raw()
    pub = base64.b64encode(priv.public_key().public_bytes_raw()).decode()
    return raw, pub


def _sign(private: bytes, data: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(private)
    return base64.b64encode(priv.sign(data)).decode()


@pytest.fixture()
def key(tmp_path, monkeypatch):
    path = tmp_path / "signing.key"
    generate_key(path)
    monkeypatch.setenv("SOMA_SIGNING_KEY", str(path))
    return path


@pytest.fixture()
def registry(tmp_path) -> Registry:
    return Registry(path=tmp_path / "agents.json", agents={})


# ── KYA ──────────────────────────────────────────────────────

def test_registration_requires_proof_of_key(registry):
    private, public = _keypair()
    nonce = registry.issue_nonce("ext-1")
    ok, why = registry.register("ext-1", public, ["research"], 0.01, proof=None)
    assert not ok and "доказательства" in why, why
    assert "ext-1" not in registry.agents


def test_registration_accepts_valid_proof(registry):
    private, public = _keypair()
    nonce = registry.issue_nonce("ext-1")
    proof = _sign(private, nonce.encode())
    ok, why = registry.register("ext-1", public, ["research"], 0.01, proof=proof)
    assert ok, why
    assert registry.public_key("ext-1") == public
    assert registry.get("ext-1")["capabilities"] == ["research"]


def test_registration_rejects_proof_from_another_key(registry):
    private, public = _keypair()
    other_private, _ = _keypair()
    nonce = registry.issue_nonce("ext-1")
    proof = _sign(other_private, nonce.encode())   # подпись чужим ключом
    ok, why = registry.register("ext-1", public, ["research"], 0.01, proof=proof)
    assert not ok and "не сошлось" in why, why
    assert "ext-1" not in registry.agents


def test_nonce_is_single_use(registry):
    private, public = _keypair()
    nonce = registry.issue_nonce("ext-1")
    proof = _sign(private, nonce.encode())
    assert registry.register("ext-1", public, [], 0.0, proof=proof)[0] is True
    # тот же нонс второй раз не работает
    assert registry.register("ext-2", public, [], 0.0, proof=proof)[0] is False


def test_registration_without_nonce_is_refused(registry):
    private, public = _keypair()
    ok, why = registry.register("ext-1", public, [], 0.0, proof=_sign(private, b"x"))
    assert not ok and "нонса" in why, why


# ── квитанции и репутация ────────────────────────────────────

def test_receipt_signs_and_verifies():
    private, public = _keypair()
    payload = receipt_payload("worker", "env-1", result_hash({"ok": True}))
    receipt = sign_receipt(payload, private)
    ok, why = verify_receipt(receipt, public)
    assert ok, why
    ok, why = verify_receipt(receipt, "другой-ключ")
    assert not ok and "не совпадает" in why


def test_reputation_counts_only_verified_receipts(tmp_path):
    store = ReputationStore(path=tmp_path / "rep.json")
    private, public = _keypair()
    payload = receipt_payload("worker", "env-1", result_hash({"ok": True}))
    ok, why = store.record_receipt(sign_receipt(payload, private), public)
    assert ok, why
    score = store.score("worker")
    assert score["verified_receipts"] == 1
    assert score["breaches"] == 0
    assert score["trust"] == "observed"


def test_unsigned_receipt_does_not_count(tmp_path):
    store = ReputationStore(path=tmp_path / "rep.json")
    payload = receipt_payload("worker", "env-1", result_hash({"ok": True}))
    ok, why = store.record_receipt({"payload": payload, "algorithm": "ed25519",
                                    "signature": "мусор", "public_key": "x"}, None)
    assert not ok, "непроверяемая квитанция не должна засчитываться"
    assert store.score("worker")["verified_receipts"] == 0


def test_breach_lowers_trust(tmp_path):
    store = ReputationStore(path=tmp_path / "rep.json")
    private, public = _keypair()
    for i in range(3):
        payload = receipt_payload("worker", f"env-{i}", result_hash({"i": i}))
        store.record_receipt(sign_receipt(payload, private), public)
    assert store.score("worker")["trust"] == "observed"
    store.record_breach("worker", "env-x", "amount")
    score = store.score("worker")
    assert score["breaches"] == 1
    assert score["weight"] == 3 - 4, "нарушение перевешивает три успеха"
    assert score["trust"] == "breached", "отрицательный вес — статус breached"


def test_reputation_survives_restart(tmp_path):
    path = tmp_path / "rep.json"
    private, public = _keypair()
    store = ReputationStore(path=path)
    payload = receipt_payload("worker", "env-1", result_hash({"ok": True}))
    store.record_receipt(sign_receipt(payload, private), public)
    again = ReputationStore(path=path)
    assert again.score("worker")["verified_receipts"] == 1


# ── хаб ──────────────────────────────────────────────────────

def test_hub_records_breach_when_mandate_denies(key, tmp_path):
    hub = Hub(registry=Registry(path=tmp_path / "agents.json", agents={}),
              reputation=ReputationStore(path=tmp_path / "rep.json"))
    from soma.envelope import Mandate
    # Мандат задаётся сразу: подписанный конверт не переписывается.
    env = Envelope(goal="g", task="t", to="worker",
                   mandate=Mandate(allowed_payees=["someone-else"]))
    hub.send(env)
    refused = hub.claim(env.id, "worker")
    assert refused["ok"] is False
    assert hub.reputation.score("worker")["breaches"] == 1


def test_hub_counts_signed_receipt(key, tmp_path):
    hub = Hub(registry=Registry(path=tmp_path / "agents.json", agents={}),
              reputation=ReputationStore(path=tmp_path / "rep.json"))
    private, public = _keypair()
    hub.registry.agents["worker"] = {"capabilities": ["research"], "cost_per_call": 0.0,
                                     "public_key": public}
    hub.registry.save()
    env = Envelope(goal="g", task="t", to="worker")
    hub.send(env)
    hub.claim(env.id, "worker")
    payload = receipt_payload("worker", env.id, result_hash({"text": "готово"}))
    receipt = sign_receipt(payload, private)
    res = hub.complete(env.id, {"text": "готово"}, "worker", receipt)
    assert res["ok"] is True, res
    assert hub.reputation.score("worker")["verified_receipts"] == 1


# ── HTTP ─────────────────────────────────────────────────────

def _serve(monkeypatch, tmp_path):
    monkeypatch.setenv("SOMA_REPUTATION", str(tmp_path / "rep.json"))
    hub = Hub(registry=Registry(path=tmp_path / "agents.json", agents={}),
              reputation=ReputationStore(path=tmp_path / "rep.json"))
    srv = make_server(hub, "127.0.0.1", 0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.2)
    return f"http://127.0.0.1:{port}"


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, json.loads(r.read().decode())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def test_http_nonce_registration_and_reputation(monkeypatch, key, tmp_path):
    base = _serve(monkeypatch, tmp_path)
    private, public = _keypair()
    code, body = _get(base + "/v1/agents/nonce?agent=ext-1")
    assert code == 200 and body["nonce"], body
    proof = _sign(private, body["nonce"].encode())
    code, body = _post(base + "/v1/agents", {"agent": "ext-1", "public_key": public,
                                             "capabilities": ["research"], "proof": proof})
    assert code == 200 and body["ok"], body
    code, body = _get(base + "/v1/agents")
    assert "ext-1" in body["agents"], body
    code, body = _get(base + "/v1/reputation?agent=ext-1")
    assert code == 200 and body["agent"] == "ext-1", body
    assert body["trust"] == "unknown", body
