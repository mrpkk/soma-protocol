"""Герметичность набора тестов протокола.

Тесты Soma проверяют **эталонные** реализации. Плагины, установленные на
машине (движки Mitra Core), не должны влиять на них: иначе набор тестов
протокола зависел бы от того, что у разработчика стоит. Плагины проверяются
отдельно — в своём пакете.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _no_plugins(monkeypatch):
    """Отключить внешние плагины на время каждого теста."""
    monkeypatch.setenv("SOMA_PLUGINS", "")
    import importlib.metadata as md
    monkeypatch.setattr(md, "entry_points", lambda group=None: [])
    yield
