"""Тесты протокола Mitra.

Проверяем не «как написано», а свойства, на которые опираются агенты:
подпись ломается при подмене, мандат отказывает с именем правила,
бюджет жёсткий, деградация объявлена, просроченное не выдаётся.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mitra.anchors import LocalChain  # noqa: E402
from mitra.envelope import (Envelope, Mandate, canonical, check_mandate,  # noqa: E402
                            generate_key, route, sign, validate, verify)
from mitra.memory import Memory  # noqa: E402
from mitra.payments import Usage, quote, x402_header_ok  # noqa: E402
from mitra.registry import Registry  # noqa: E402
from mitra.server import Hub  # noqa: E402


@pytest.fixture(scope="session")
def key(tmp_path_factory) -> bytes:
    path = tmp_path_factory.mktemp("keys") / "signing.key"
    generate_key(path)
    os.environ["MITRA_SIGNING_KEY"] = str(path)
    return path.read_bytes()


@pytest.fixture()
def memory(tmp_path) -> Memory:
    return Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold")


@pytest.fixture()
def hub(memory) -> Hub:
    return Hub(memory, Registry(agents={
        "research": {"capabilities": ["research"], "cost_per_call": 0.01},
        "coder": {"capabilities": ["code"], "cost_per_call": 0.02},
    }))


# ── конверт ───────────────────────────────────────────────────

def test_valid_envelope_passes_schema():
    env = Envelope(goal="проверить артефакт", task="прогнать линтер")
    assert validate(env) == []


def test_schema_rejects_empty_goal():
    env = Envelope(goal="", task="что-то")
    errors = validate(env)
    assert any("goal" in e for e in errors), errors


def test_canonical_is_stable():
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical(a) == canonical(b)


def test_signature_roundtrip(key):
    env = Envelope(goal="g", task="t")
    sign(env)
    ok, why = verify(env)
    assert ok, why
    assert env.signature["algorithm"] == "ed25519"


def test_tampered_envelope_fails_verification(key):
    env = Envelope(goal="g", task="t")
    sign(env)
    env.task = "t подменённая"
    ok, _ = verify(env)
    assert not ok, "подпись должна ломаться при подмене задачи"


def test_unsigned_envelope_is_reported_as_unsigned():
    env = Envelope(goal="g", task="t")
    ok, why = verify(env)
    assert not ok and "не подписан" in why


# ── мандат ───────────────────────────────────────────────────

def test_mandate_allows_within_limits():
    m = Mandate(max_cost_usd=10.0, max_calls=3, allowed_payees=["alice"])
    ok, why = check_mandate({"amount": 5.0, "payee": "alice", "calls": 1}, m)
    assert ok, why


def test_mandate_denies_amount_and_names_rule():
    m = Mandate(max_cost_usd=10.0)
    ok, why = check_mandate({"amount": 50.0}, m)
    assert not ok and why.startswith("amount:"), why


def test_mandate_denies_payee():
    m = Mandate(allowed_payees=["alice"])
    ok, why = check_mandate({"payee": "mallory"}, m)
    assert not ok and why.startswith("payee:"), why


def test_mandate_denies_expired():
    m = Mandate(expires_at=time.time() - 1)
    ok, why = check_mandate({"at": time.time()}, m)
    assert not ok and why.startswith("validity_window:"), why


def test_no_mandate_means_no_restriction():
    ok, why = check_mandate({"amount": 10_000}, None)
    assert ok, why


# ── роутер ──────────────────────────────────────────────────

def test_router_picks_cheapest_matching_capability():
    env = Envelope(goal="g", task="t", capability=["research"])
    d = route(env, {"research": {"capabilities": ["research"], "cost_per_call": 0.02},
                    "coder": {"capabilities": ["code"], "cost_per_call": 0.01}})
    assert d["agent"] == "research", d
    assert any("coder" in r for r in d["rejected"]), d


def test_router_refuses_without_candidate_and_explains():
    env = Envelope(goal="g", task="t", capability=["translate"])
    d = route(env, {"coder": {"capabilities": ["code"], "cost_per_call": 0.01}})
    assert d["agent"] is None and d["rejected"], d


def test_router_respects_price_ceiling():
    env = Envelope(goal="g", task="t", capability=["research"], price_usdc=0.005)
    d = route(env, {"research": {"capabilities": ["research"], "cost_per_call": 0.01}})
    assert d["agent"] is None, d


# ── память ───────────────────────────────────────────────────

def test_inbox_delivers_to_addressed_agent(memory):
    memory.put(Envelope(goal="g", task="t", to="alice"))
    memory.put(Envelope(goal="g", task="t", to="bob"))
    assert [e["to"] for e in memory.inbox("alice")] == ["alice"]


def test_broadcast_reaches_any_agent(memory):
    memory.put(Envelope(goal="g", task="t", to="*"))
    assert len(memory.inbox("anyone")) == 1


def test_claim_is_exclusive(memory):
    memory.put(Envelope(goal="g", task="t", to="alice"))
    env_id = memory.inbox("alice")[0]["id"]
    first, _ = memory.claim(env_id, "alice")
    second, why = memory.claim(env_id, "bob")
    assert first is not None
    assert second is None and "недоступен" in why


def test_expired_envelope_is_not_delivered(memory):
    memory.put(Envelope(goal="g", task="t", to="alice", ttl_s=0.01))
    time.sleep(0.02)
    assert memory.inbox("alice") == []
    assert memory.stats()["by_status"].get("expired") == 1


def test_context_respects_token_budget(memory):
    for i in range(30):
        memory.remember(f"факт {i}: очень длинный текст про бюджет токенов " * 5)
    env = Envelope(goal="g", task="t", token_budget=120, context_query="бюджет токенов")
    memory.put(env)
    packed = memory.pack(memory.get(env.id))
    assert packed["tokens"] <= packed["budget"], packed
    assert packed["sources"], packed


def test_context_reports_what_did_not_fit(memory):
    for i in range(30):
        memory.remember(f"факт {i}: очень длинный текст про бюджет токенов " * 5)
    env = Envelope(goal="g", task="t", token_budget=60, context_query="бюджет токенов")
    memory.put(env)
    packed = memory.pack(memory.get(env.id))
    assert packed["dropped"], "деградация должна быть объявлена, а не скрыта"


def test_cold_ref_is_included_by_hash(memory):
    ref = memory.put_cold("важный артефакт")
    env = Envelope(goal="g", task="t", cold_refs=[ref], token_budget=500)
    memory.put(env)
    packed = memory.pack(memory.get(env.id))
    assert "cold" in packed["sources"], packed
    assert ref in packed["text"]


def test_missing_cold_ref_is_reported(memory):
    env = Envelope(goal="g", task="t", cold_refs=["sha256:deadbeef"], token_budget=500)
    memory.put(env)
    packed = memory.pack(memory.get(env.id))
    assert any("cold" in d for d in packed["dropped"]), packed


# ── история ──────────────────────────────────────────────────

def test_anchor_chain_is_append_only_and_verifiable(tmp_path):
    chain = LocalChain(tmp_path / "history.jsonl")
    r1 = chain.append({"what": "первое"})
    r2 = chain.append({"what": "второе"})
    assert r2["prev"] == r1["hash"]
    assert chain.verify(r1) and chain.verify(r2)
    r2["payload"]["what"] = "подменено"
    assert not chain.verify(r2)


# ── деньги ───────────────────────────────────────────────────

def test_usage_cost_is_measured_not_priced():
    u = Usage(tokens=1000, cpu_seconds=0.5, external_calls=1)
    q = quote(u)
    assert q["currency"] == "USD" and q["cost_usd"] > 0
    assert q["cost_usd"] < 0.01, "проверка не должна стоить как аренда сервера"


def test_x402_header_contract():
    assert x402_header_ok({}, 0.005)[0] is False
    assert x402_header_ok({"x-payment": "receipt"}, 0.005)[0] is True


# ── хаб целиком ──────────────────────────────────────────────

def test_hub_roundtrip(hub):
    env = Envelope(goal="проверить", task="сделать", to="research", capability=["research"])
    res = hub.send(env)
    assert res["ok"], res
    inbox = hub.inbox("research")
    assert inbox and inbox[0]["signature_ok"], inbox
    claim = hub.claim(env.id, "research")
    assert claim["ok"], claim
    assert hub.complete(env.id, {"text": "готово"}, "research")["ok"]
    assert hub.memory.get(env.id)["status"] == "done"
