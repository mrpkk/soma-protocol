"""Mitra — реестр исполнителей.

Файл, а не база: реестр должен переезжать вместе с агентом и быть читаем
глазами. Каждая запись — кто, что умеет (capability), сколько стоит вызов.

Формат (JSON):
    {"agent": {"capabilities": ["research"], "cost_per_call": 0.01}}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

def default_path() -> Path:
    return Path(os.environ.get("MITRA_AGENTS", Path.home() / ".mitra" / "agents.json"))

#: Заготовка реестра на старте: встроенные роли без внешних зависимостей.
DEFAULT_AGENTS: dict[str, Any] = {
    "research": {"capabilities": ["research", "search", "analysis"], "cost_per_call": 0.01},
    "coder": {"capabilities": ["code", "refactor", "test"], "cost_per_call": 0.02},
    "verifier": {"capabilities": ["verify", "audit"], "cost_per_call": 0.01},
    "builder": {"capabilities": ["build", "plan"], "cost_per_call": 0.02},
}


class Registry:
    """Реестр агентов. Читается из файла, при отсутствии — встроенный."""

    def __init__(self, path: str | Path | None = None, agents: dict[str, Any] | None = None):
        self.path = Path(path) if path else default_path()
        if agents is not None:
            self.agents = agents
        elif self.path.exists():
            try:
                self.agents = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                # Молча подменять реестр дефолтным нельзя: маршрутизация поедет
                # незаметно. Ошибка — наверх.
                raise ValueError(f"реестр агентов не читается: {self.path} ({exc})") from exc
        else:
            self.agents = dict(DEFAULT_AGENTS)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.agents, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, name: str) -> dict[str, Any] | None:
        return self.agents.get(name)

    def public_key(self, agent: str) -> str | None:
        """Публичный ключ агента из реестра (если заведён).

        С ним проверка подписи доказывает происхождение, а не только целостность.
        """
        spec = self.agents.get(agent) or {}
        return spec.get("public_key") or None

    def agent_card(self, name: str, url: str) -> dict[str, Any]:
        """Карточка агента в формате A2A (agents.json) — чтобы нас находили."""
        spec = self.agents.get(name, {})
        return {
            "name": name,
            "description": f"Агент Mitra: {', '.join(spec.get('capabilities', [])) or 'общий'}",
            "url": url,
            "version": "1.0.0",
            "capabilities": spec.get("capabilities", []),
            "provider": {"organization": "Mitra", "url": "https://mitra.dev"},
            "protocol": "mitra/1",
            "skills": [{"id": c, "name": c} for c in spec.get("capabilities", [])],
        }
