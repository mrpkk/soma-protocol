"""S5: реестр внешних агентов — регистрация только с доказательством ключа."""
from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from soma.registry import Registry


def _keypair():
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return k, pub


def test_registration_requires_proof_of_key_ownership(tmp_path):
    reg = Registry(path=tmp_path / "r.json")
    reg.issue_nonce("a1")
    ok, why = reg.register("a1", "AAAA", capabilities=["x"])
    assert not ok and "доказательство" in why


def test_registration_without_nonce_is_refused(tmp_path):
    reg = Registry(path=tmp_path / "r.json")
    _, pub = _keypair()
    ok, why = reg.register("a2", pub, capabilities=["x"], proof="AAAA")
    assert not ok and "нонс" in why


def test_registration_with_valid_proof_succeeds(tmp_path):
    reg = Registry(path=tmp_path / "r.json")
    key, pub = _keypair()
    nonce = reg.issue_nonce("a3")
    proof = base64.b64encode(key.sign(nonce.encode())).decode()
    ok, why = reg.register("a3", pub, capabilities=["research"], cost_per_call=0.02, proof=proof)
    assert ok, why
    assert reg.public_key("a3") == pub
    assert reg.agents["a3"]["external"] is True


def test_proof_from_another_key_is_refused(tmp_path):
    reg = Registry(path=tmp_path / "r.json")
    _, pub_a = _keypair()
    other, _ = _keypair()
    nonce = reg.issue_nonce("a4")
    proof = base64.b64encode(other.sign(nonce.encode())).decode()
    ok, _ = reg.register("a4", pub_a, capabilities=["x"], proof=proof)
    assert not ok


def test_agent_card_is_public_and_has_skills(tmp_path):
    reg = Registry(path=tmp_path / "r.json")
    key, pub = _keypair()
    nonce = reg.issue_nonce("a5")
    reg.register("a5", pub, capabilities=["audit", "verify"],
                 proof=base64.b64encode(key.sign(nonce.encode())).decode())
    card = reg.agent_card("a5", "https://rta.lat/hub/agents/a5")
    assert card["name"] == "a5"
    assert {s["id"] for s in card["skills"]} == {"audit", "verify"}
    assert "public_key" not in card  # ключ не уходит в публичную карточку
