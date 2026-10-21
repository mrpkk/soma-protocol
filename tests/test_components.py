"""Проверка швов и плагинов (S1).

Смысл ступени: протокол открыт, движки — наша экономика. Проверяем, что шов
действительно подменяется плагином, а не просто объявлен в интерфейсе.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from soma.components import PLUGIN_ENV, build_components, discover_plugins  # noqa: E402
from soma.envelope import Envelope, Mandate  # noqa: E402
from soma.interfaces import (Anchor, MandateEvaluator, MemoryTier, Meter,  # noqa: E402
                              Signer)
from soma.server import Hub  # noqa: E402

PLUGIN_SRC = textwrap.dedent('''
    """Пример внешнего движка: свой шов мандата (движок Mitra Core)."""
    from soma.envelope import Mandate

    class StrictMandate:
        name = "mitra-core-strict"

        def check(self, action, mandate, only=None):
            # Настоящий движок знает больше правил и умеет отказать
            # даже без явного мандата в конверте.
            if action.get("payee") == "mallory":
                return False, "payee: запрещён движком Mitra Core"
            return True, "ok"

    def components():
        return {"mandate": StrictMandate()}
''')


@pytest.fixture()
def key(tmp_path, monkeypatch):
    """Ключ подписи: без него хаб честно отказывается подписывать."""
    from soma.envelope import generate_key
    path = tmp_path / "signing.key"
    generate_key(path)
    monkeypatch.setenv("SOMA_SIGNING_KEY", str(path))
    return path


@pytest.fixture()
def plugin(tmp_path, monkeypatch):
    (tmp_path / "fake_engine.py").write_text(PLUGIN_SRC, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(PLUGIN_ENV, "fake_engine:components")
    yield "fake_engine:components"
    monkeypatch.delenv(PLUGIN_ENV, raising=False)


def test_default_components_are_reference_implementations():
    comps = build_components()
    described = comps.describe()
    assert described["signer"] == "ed25519"
    assert described["mandate"] == "rules"
    assert described["meter"] == "usage"
    assert described["plugins"] == []


def test_reference_implementations_satisfy_the_interfaces():
    comps = build_components()
    assert isinstance(comps.signer, Signer)
    assert isinstance(comps.mandate, MandateEvaluator)
    assert isinstance(comps.memory, MemoryTier)
    assert isinstance(comps.meter, Meter)
    assert isinstance(comps.anchor, Anchor)


def test_plugin_replaces_a_seam(plugin, key):
    comps = build_components()
    assert comps.describe()["mandate"] == "mitra-core-strict"
    assert comps.describe()["plugins"] == [plugin]


def test_plugin_actually_changes_behaviour(plugin, key):
    """Плагин должен менять поведение хаба, а не только подпись в описании."""
    comps = build_components()
    hub = Hub(components=comps)
    env = Envelope(goal="g", task="t", to="mallory")
    assert hub.send(env)["ok"]
    refused = hub.claim(env.id, "mallory")
    assert refused["ok"] is False
    assert "Mitra Core" in refused["reason"], refused
    # и конверт не сгорел
    assert hub.memory.get(env.id)["status"] == "queued"


def test_broken_plugin_does_not_break_the_hub(tmp_path, monkeypatch):
    (tmp_path / "broken.py").write_text("raise RuntimeError('сломан')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv(PLUGIN_ENV, "broken:components")
    comps = build_components()
    assert comps.describe()["plugins"] == []


def test_discover_finds_env_plugins(plugin):
    found = discover_plugins()
    assert any(spec == plugin for spec, _ in found)


def test_meter_prices_by_actual_usage():
    comps = build_components()
    q = comps.meter.quote({"tokens": 1000, "cpu_seconds": 0.5, "external_calls": 1})
    assert q["currency"] == "USD" and q["cost_usd"] > 0
    assert q["cost_usd"] < 0.01, "проверка не должна стоить как аренда сервера"


def test_components_describe_is_reported_by_healthz(tmp_path):
    hub = Hub(memory=None)
    comps = hub.components.describe()
    assert set(comps) >= {"signer", "mandate", "memory", "meter", "anchor", "plugins"}


def test_protocol_does_not_depend_on_our_engines():
    """Эталонный шов работает без наших движков и отказывает по умолчанию."""
    comps = build_components()
    # правило включено, а поля нет — отказ, а не «ноль» (deny by default)
    ok, why = comps.mandate.check({"payee": "alice"}, Mandate(max_cost_usd=1.0))
    assert not ok and why.startswith("amount:"), why
    # с полем — по правилам
    ok, why = comps.mandate.check({"payee": "alice", "amount": 0.5}, Mandate(max_cost_usd=1.0))
    assert ok, why
