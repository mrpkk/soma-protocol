"""Набор тестов соответствия Mitra.

Это главный артефакт открытого проекта: **любая реализация хаба может пройти
этот набор** и доказать, что говорит с агентами на одном языке.

Своя реализация подключается переменной окружения:

    MITRA_CONFORMANCE_IMPL="my_package.hub:MyHubFactory"

Фабрика должна вернуть объект с методами: send, inbox, claim, complete, route,
verify, stats. Всё остальное — в тестах ниже.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Protocol

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class Harness(Protocol):
    """Минимальный контракт, который должна удовлетворять реализация хаба."""

    def send(self, **kwargs: Any) -> dict[str, Any]: ...
    def inbox(self, agent: str) -> list[dict[str, Any]]: ...
    def claim(self, env_id: str, agent: str) -> dict[str, Any]: ...
    def complete(self, env_id: str, result: dict[str, Any], agent: str | None = None) -> dict[str, Any]: ...
    def route(self, env_id: str) -> dict[str, Any]: ...
    def verify(self, envelope: dict[str, Any]) -> tuple[bool, str]: ...
    def stats(self) -> dict[str, Any]: ...


class MitraHarness:
    """Обёртка над эталонной реализацией — по умолчанию."""

    def __init__(self, tmp_path: Path):
        from mitra.envelope import generate_key
        from mitra.memory import Memory
        from mitra.registry import Registry
        from mitra.server import Hub

        key = tmp_path / "signing.key"
        generate_key(key)
        os.environ["MITRA_SIGNING_KEY"] = str(key)
        self.hub = Hub(Memory(path=tmp_path / "m.db", cold_dir=tmp_path / "cold"),
                       Registry(agents={"research": {"capabilities": ["research"],
                                                     "cost_per_call": 0.01}}))

    def send(self, **kwargs: Any) -> dict[str, Any]:
        from mitra.envelope import Envelope
        env = Envelope(**kwargs)
        return self.hub.send(env)

    def inbox(self, agent: str) -> list[dict[str, Any]]:
        return self.hub.inbox(agent)

    def claim(self, env_id: str, agent: str) -> dict[str, Any]:
        return self.hub.claim(env_id, agent)

    def complete(self, env_id: str, result: dict[str, Any], agent: str | None = None) -> dict[str, Any]:
        return self.hub.complete(env_id, result, agent)

    def route(self, env_id: str) -> dict[str, Any]:
        return self.hub.route(env_id)

    def verify(self, envelope: dict[str, Any]) -> tuple[bool, str]:
        from mitra.envelope import Envelope, verify
        return verify(Envelope.from_dict(envelope))

    def stats(self) -> dict[str, Any]:
        return self.hub.memory.stats()


def _load_external() -> Any:
    spec = os.environ.get("MITRA_CONFORMANCE_IMPL")
    if not spec:
        return None
    mod, _, attr = spec.partition(":")
    import importlib
    factory = getattr(importlib.import_module(mod), attr)
    return factory


@pytest.fixture()
def impl(tmp_path):
    factory = _load_external()
    return factory(tmp_path) if factory else MitraHarness(tmp_path)


# ── соответствие ─────────────────────────────────────────────

def test_c1_send_returns_envelope_id(impl):
    res = impl.send(goal="g", task="t", to="research")
    assert res.get("ok") is True, res
    assert res.get("id"), res


def test_c2_envelope_is_signed_and_verifies(impl):
    res = impl.send(goal="g", task="t", to="research")
    items = impl.inbox("research")
    assert items, "конверт должен быть доставлен"
    env = items[0]["envelope"]
    ok, why = impl.verify(env)
    assert ok, f"подпись не прошла проверку: {why}"


def test_c3_tampering_breaks_the_signature(impl):
    impl.send(goal="g", task="t", to="research")
    env = dict(impl.inbox("research")[0]["envelope"])
    env["task"] = env["task"] + " (подменено)"
    ok, _ = impl.verify(env)
    assert not ok, "подпись должна ломаться при подмене"


def test_c4_context_respects_the_declared_budget(impl):
    impl.send(goal="g", task="t", to="research", token_budget=100, context_query="бюджет")
    ctx = impl.inbox("research")[0]["context"]
    assert ctx["tokens"] <= ctx["budget"], ctx


def test_c5_degradation_is_declared(impl):
    impl.send(goal="g", task="t", to="research", token_budget=50, context_query="бюджет токенов")
    ctx = impl.inbox("research")[0]["context"]
    assert "dropped" in ctx, "реализация обязана сообщать, что не влезло"
    assert isinstance(ctx["dropped"], list)


def test_c6_expired_envelope_is_not_delivered(impl):
    impl.send(goal="g", task="t", to="research", ttl_s=0.01)
    time.sleep(0.05)
    assert not [i for i in impl.inbox("research") if i["envelope"]["status"] != "expired"]


def test_c7_claim_is_exclusive(impl):
    res = impl.send(goal="g", task="t", to="research")
    first = impl.claim(res["id"], "research")
    second = impl.claim(res["id"], "other")
    assert first.get("ok") is True, first
    assert second.get("ok") is not True, "второй претендент не должен получить конверт"


def test_c8_route_refusal_is_explained(impl):
    res = impl.send(goal="g", task="t", to="research", capability=["несуществующая-способность"])
    decision = impl.route(res["id"])
    if decision.get("agent") is None:
        assert decision.get("reason"), "отказ обязан объясняться"
    else:
        assert decision.get("reason"), "и выбор обязан объясняться"


def test_c9_completion_closes_the_envelope(impl):
    res = impl.send(goal="g", task="t", to="research")
    assert impl.complete(res["id"], {"text": "готово"}, "research").get("ok") is True
    stats = impl.stats()
    assert stats.get("by_status", {}).get("done", 0) >= 1, stats


def test_c10_budget_and_mandate_travel_with_the_envelope(impl):
    res = impl.send(goal="g", task="t", to="research", token_budget=777,
                    mandate={"max_cost_usd": 1.5})
    env = impl.inbox("research")[0]["envelope"]
    assert env.get("token_budget") == 777, env
    assert (env.get("mandate") or {}).get("max_cost_usd") == 1.5, env
