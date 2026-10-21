"""Soma — сборка компонентов и загрузка плагинов.

Ядро работает на эталонных реализациях. Любой компонент можно заменить
плагином — локальным (переменная `SOMA_PLUGINS="модуль:фабрика"`) или
установленным пакетом, который зарегистрировал себя через точку входа
группы `soma.plugins`.

Фабрика плагина возвращает словарь с любым из ключей:
    signer | mandate | memory | meter | anchor | registry
Значения — объекты, удовлетворяющие интерфейсам из `soma.interfaces`.

Так Mitra Core (attest, drift, agentpay, indra-memory) подключается к
протоколу, оставаясь закрытым: протокол открыт, движки — нет.
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from .anchors import LocalChain
from .envelope import Ed25519Signer, RuleMandate
from .interfaces import Anchor, MandateEvaluator, MemoryTier, Meter, Signer
from .memory import Memory
from .payments import UsageMeter
from .registry import Registry

PLUGIN_ENV = "SOMA_PLUGINS"
ENTRY_POINT_GROUP = "soma.plugins"


@dataclass
class Components:
    """Что подключено. Собирается из эталонов и плагинов."""

    signer: Signer
    mandate: MandateEvaluator
    memory: MemoryTier
    meter: Meter
    anchor: Anchor
    registry: Registry
    plugins: list[str] = field(default_factory=list)

    def describe(self) -> dict[str, Any]:
        """Кто подключён и откуда — для /healthz и для карты разграничения."""
        return {
            "signer": getattr(self.signer, "name", type(self.signer).__name__),
            "mandate": getattr(self.mandate, "name", type(self.mandate).__name__),
            "memory": type(self.memory).__name__,
            "meter": getattr(self.meter, "name", type(self.meter).__name__),
            "anchor": type(self.anchor).__name__,
            "registry": len(self.registry.agents),
            "plugins": self.plugins,
        }


def _load_factory(spec: str) -> Callable[[], dict[str, Any]]:
    """«модуль:атрибут» → фабрика."""
    mod, _, attr = spec.partition(":")
    if not attr:
        attr = "components"
    obj = getattr(importlib.import_module(mod), attr)
    return obj if callable(obj) else (lambda: obj)


def discover_plugins() -> list[tuple[str, Callable[[], dict[str, Any]]]]:
    """Плагины из `SOMA_PLUGINS` и из установленных пакетов (точки входа)."""
    found: list[tuple[str, Callable[[], dict[str, Any]]]] = []
    raw = os.environ.get(PLUGIN_ENV, "")
    for spec in (s.strip() for s in raw.split(",")):
        if not spec:
            continue
        try:
            found.append((spec, _load_factory(spec)))
        except Exception:
            continue  # битый плагин не должен ломать хаб
    try:
        from importlib.metadata import entry_points
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                # ep.load() возвращает ОБЪЕКТ (часто — функцию-фабрику),
                # а не словарь компонентов. Приводим к единому виду «вызвать → dict».
                def _make(ep=ep):
                    obj = ep.load()
                    return obj() if callable(obj) else obj
                found.append((f"{ep.name} ({ep.value})", _make))
            except Exception:
                continue
    except Exception:
        pass
    return found


def build_components(memory: MemoryTier | None = None,
                     registry: Registry | None = None,
                     plugins: list[tuple[str, Callable[[], dict[str, Any]]]] | None = None) -> Components:
    """Собрать компоненты: эталоны → плагины (плагин может заменить любой шов)."""
    mem = memory if memory is not None else Memory()
    comps = Components(
        signer=Ed25519Signer(),
        mandate=RuleMandate(),
        memory=mem,
        meter=UsageMeter(),
        anchor=LocalChain(),
        registry=registry if registry is not None else Registry(),
    )
    names: list[str] = []
    for name, factory in (plugins if plugins is not None else discover_plugins()):
        try:
            provided = factory() or {}
        except Exception:
            continue
        for key in ("signer", "mandate", "memory", "meter", "anchor", "registry"):
            if key in provided:
                setattr(comps, key, provided[key])
        names.append(name)
    comps.plugins = names
    return comps
