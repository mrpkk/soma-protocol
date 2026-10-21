"""Mitra — реестр исполнителей.

Файл, а не база: реестр должен переезжать вместе с агентом и быть читаем
глазами. Каждая запись — кто, что умеет (capability), сколько стоит вызов.

Формат (JSON):
    {"agent": {"capabilities": ["research"], "cost_per_call": 0.01}}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

def default_path() -> Path:
    return Path(os.environ.get("SOMA_AGENTS", Path.home() / ".soma" / "agents.json"))

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

    # ── KYA: агент доказывает, что ключ его ───────────────────
    def issue_nonce(self, agent: str) -> str:
        """Выдать одноразовый нонс: агент подписывает его своим ключом."""
        import os
        import secrets
        pending = self.path.with_suffix(".pending.json")
        data: dict[str, str] = {}
        if pending.exists():
            try:
                data = json.loads(pending.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        nonce = secrets.token_hex(16)
        data[agent] = nonce
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return nonce

    def _take_nonce(self, agent: str) -> str | None:
        pending = self.path.with_suffix(".pending.json")
        if not pending.exists():
            return None
        try:
            data = json.loads(pending.read_text(encoding="utf-8"))
        except Exception:
            return None
        nonce = data.pop(agent, None)
        pending.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return nonce

    def register(self, agent: str, public_key: str, capabilities: list[str] | None = None,
                 cost_per_call: float = 0.0, proof: str | None = None,
                 metadata: dict[str, Any] | None = None) -> tuple[bool, str]:
        """Зарегистрировать внешнего агента.

        Требуется доказательство владения ключом: подпись одноразового нонса
        тем самым ключом, который агент заявляет. Без этого в реестр нельзя
        попасть — иначе в реестре можно было бы написать чужой ключ.
        """
        import base64
        nonce = self._take_nonce(agent)
        if not nonce:
            return False, "нет нонса для этого агента (запросите его)"
        if not proof:
            return False, "нет доказательства владения ключом"
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
            Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key)).verify(
                base64.b64decode(proof), nonce.encode("utf-8"))
        except Exception as exc:
            return False, f"доказательство не сошлось ({type(exc).__name__})"
        self.agents[agent] = {
            "capabilities": list(capabilities or []),
            "cost_per_call": float(cost_per_call),
            "public_key": public_key,
            "registered_at": time.time(),
            "external": True,
            "metadata": metadata or {},
        }
        self.save()
        return True, "ok"

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
            "provider": {"organization": "Soma", "url": "https://soma.dev"},
            "protocol": "soma/1",
            "skills": [{"id": c, "name": c} for c in spec.get("capabilities", [])],
        }
