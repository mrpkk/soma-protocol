"""Soma — адаптер A2A.

A2A (Agent-to-Agent) — открытый стандарт общения агентов: задача, контекст,
сообщения с частями, карточка агента. Наш конверт отображается в задачу A2A
и обратно: перевозчик чужой, письмо наше.

Схема соответствия:
    конверт.id        → taskId / contextId
    конверт.goal      → сообщение A2A (первая часть) + метаданные
    конверт.task      → текст задачи
    конверт.budget    → metadata.soma.token_budget
    конверт.мандат   → metadata.soma.mandate
    конверt.подпись  → metadata.soma.signature
    результат         → сообщение A2A с текстом ответа

Зависимостей нет: это перевод словарей, а не клиент A2A.
"""

from __future__ import annotations

from typing import Any

from .envelope import Envelope

PROTOCOL_VERSION = "0.3.0"   # версия протокола A2A, которую мы понимаем


def envelope_to_task(envelope: Envelope, context_id: str | None = None) -> dict[str, Any]:
    """Конверт → задача A2A."""
    context = context_id or f"ctx-{envelope.id}"
    payload = envelope.to_dict()
    return {
        "id": envelope.id,
        "contextId": context,
        "kind": "task",
        "status": {"state": "submitted", "timestamp": None},
        "metadata": {"soma": payload},
        "artifacts": [],
        "history": [
            {
                "role": "user",
                "parts": [{"kind": "text", "text": f"{envelope.goal}\n\n{envelope.task}"}],
                "messageId": f"{envelope.id}-m0",
            }
        ],
    }


def task_to_envelope(task: dict[str, Any]) -> Envelope:
    """Задача A2A → конверт. Берём сохранённый конверт из метаданных."""
    meta = (task.get("metadata") or {}).get("soma")
    if not meta:
        raise ValueError("в задаче нет сохранённого конверта (metadata.soma)")
    env = Envelope.from_dict(meta)
    if task.get("id"):
        env.id = task["id"]
    return env


def envelope_to_message(envelope: Envelope) -> dict[str, Any]:
    """Конверт → сообщение A2A (для потоковой доставки)."""
    return {
        "kind": "message",
        "messageId": f"{envelope.id}-m0",
        "role": "user",
        "parts": [{"kind": "text", "text": f"{envelope.goal}\n\n{envelope.task}"}],
        "metadata": {"soma": envelope.to_dict()},
    }


def result_to_message(envelope_id: str, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Результат работы → сообщение A2A."""
    return {
        "kind": "message",
        "messageId": f"{envelope_id}-r0",
        "role": "agent",
        "parts": [{"kind": "text", "text": text}],
        "metadata": metadata or {},
    }


def task_from_result(envelope_id: str, text: str, context_id: str | None = None) -> dict[str, Any]:
    """Результат → завершённая задача A2A."""
    return {
        "id": envelope_id,
        "contextId": context_id or f"ctx-{envelope_id}",
        "kind": "task",
        "status": {"state": "completed", "timestamp": None},
        "artifacts": [{"name": "result", "parts": [{"kind": "text", "text": text}]}],
        "history": [{"role": "agent", "parts": [{"kind": "text", "text": text}],
                     "messageId": f"{envelope_id}-r0"}],
    }


def agent_card(name: str, url: str, description: str, skills: list[str],
               version: str = "1.1.0") -> dict[str, Any]:
    """Карточка агента в формате A2A (для обнаружения)."""
    return {
        "name": name,
        "description": description,
        "url": url,
        "version": version,
        "protocolVersion": PROTOCOL_VERSION,
        "preferredTransport": "JSONRPC",
        "capabilities": {"streaming": False, "pushNotifications": False, "stateTransitionHistory": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [{"id": s, "name": s, "description": s} for s in skills],
        "provider": {"organization": "Soma", "url": "https://github.com/mrpkk/soma-protocol"},
    }
