"""S3: адаптер A2A и приём оплаты по конверту.

Проверяем два свойства:
- конверт не теряется при перевозке через A2A (задача → конверт → задача);
- работа, которой назначена цена, не выдаётся без квитанции об оплате.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from soma.a2a import (agent_card, envelope_to_message, envelope_to_task,  # noqa: E402
                       result_to_message, task_from_result, task_to_envelope)
from soma.envelope import Envelope, generate_key  # noqa: E402
from soma.payments import check_receipt  # noqa: E402
from soma.server import Hub  # noqa: E402


@pytest.fixture()
def key(tmp_path, monkeypatch):
    path = tmp_path / "signing.key"
    generate_key(path)
    monkeypatch.setenv("SOMA_SIGNING_KEY", str(path))
    return path


# ── A2A ──────────────────────────────────────────────────────

def test_envelope_survives_a2a_roundtrip():
    env = Envelope(goal="проверить артефакт", task="прогнать линтер", to="verifier",
                   capability=["verify"], token_budget=1200, cost_usdc=0.005)
    task = envelope_to_task(env)
    assert task["kind"] == "task"
    assert task["id"] == env.id
    back = task_to_envelope(task)
    assert back.id == env.id
    assert back.goal == env.goal and back.task == env.task
    assert back.token_budget == 1200
    assert back.capability == ["verify"]
    assert back.cost_usdc == 0.005
    assert back.signature == env.signature


def test_a2a_task_carries_the_envelope_in_metadata():
    env = Envelope(goal="g", task="t")
    task = envelope_to_task(env)
    assert task["metadata"]["soma"]["goal"] == "g"
    assert task["history"][0]["parts"][0]["text"].startswith("g")


def test_a2a_message_and_result():
    env = Envelope(goal="g", task="t")
    msg = envelope_to_message(env)
    assert msg["kind"] == "message" and msg["role"] == "user"
    res = result_to_message(env.id, "готово")
    assert res["role"] == "agent" and res["parts"][0]["text"] == "готово"
    task = task_from_result(env.id, "готово")
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "готово"


def test_agent_card_is_discoverable():
    card = agent_card("soma", "https://example/mcp", "протокол", ["send", "claim"])
    assert card["protocolVersion"]
    assert card["capabilities"]["stateTransitionHistory"] is True
    assert card["skills"][0]["id"] == "send"


def test_task_without_soma_metadata_is_refused():
    with pytest.raises(ValueError):
        task_to_envelope({"id": "x", "kind": "task"})


# ── оплата ───────────────────────────────────────────────────

def test_receipt_must_cover_the_price():
    ok, why = check_receipt(json.dumps({"amount": 0.001}), 0.005)
    assert not ok and "меньше требуемой" in why
    ok, why = check_receipt(json.dumps({"amount": 0.005}), 0.005)
    assert ok, why
    ok, why = check_receipt(None, 0.005)
    assert not ok and "не предъявлена" in why


def test_receipt_checks_asset_and_payee():
    ok, why = check_receipt(json.dumps({"amount": 0.005, "asset": "ETH"}), 0.005)
    assert not ok and "не USDC" in why
    ok, why = check_receipt(json.dumps({"amount": 0.005, "payee": "0xbad"}), 0.005, payee="0xgood")
    assert not ok and "получатель" in why


def test_paid_work_is_not_delivered_without_receipt(key):
    hub = Hub()
    env = Envelope(goal="g", task="t", to="worker", cost_usdc=0.005)
    assert hub.send(env)["ok"]
    refused = hub.claim(env.id, "worker")
    assert refused["ok"] is False
    assert refused["code"] == "payment_required"
    assert refused["amount_usdc"] == 0.005
    # и конверт не сгорел
    assert hub.memory.get(env.id)["status"] == "queued"


def test_paid_work_is_delivered_with_a_receipt(key):
    hub = Hub()
    env = Envelope(goal="g", task="t", to="worker", cost_usdc=0.005)
    hub.send(env)
    ok = hub.claim(env.id, "worker", json.dumps({"amount": 0.005, "asset": "USDC"}))
    assert ok["ok"], ok
    assert ok["envelope"]["status"] == "claimed"


def test_free_work_needs_no_receipt(key):
    hub = Hub()
    env = Envelope(goal="g", task="t", to="worker")
    hub.send(env)
    assert hub.claim(env.id, "worker")["ok"]
