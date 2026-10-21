"""Soma — швы (интерфейсы).

Протокол не диктует, *как* считать, — он диктует, *что* должно быть посчитано.
Пять швов:

    Signer           — кто подписал конверт и как это проверить
    MandateEvaluator — разрешено ли действие (правила могут быть любыми)
    MemoryTier       — где лежит память и как собирается контекст
    Meter            — сколько действие стоило на самом деле
    Anchor           — где закрепляется история

Реализации в OSS — эталонные и минимальные. Настоящие движки (Mitra Core:
attest, drift, agentpay, indra-memory) подключаются плагинами и в OSS не
попадают. Это и есть граница: форма открыта, ум — наш.

Любая реализация структурно совместима: достаточно иметь эти методы.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .envelope import Envelope, Mandate


@runtime_checkable
class Signer(Protocol):
    """Подпись конверта и её проверка."""

    name: str

    def sign(self, envelope: Envelope) -> dict[str, Any]:
        """Подписать конверт на месте. Возвращает словарь подписи."""
        ...

    def verify(self, envelope: Envelope,
               expected_public_key: str | None = None) -> tuple[bool, str]:
        """Проверить подпись. Возвращает (ok, причина)."""
        ...


@runtime_checkable
class MandateEvaluator(Protocol):
    """Проверка действия против мандата."""

    name: str

    def check(self, action: dict[str, Any], mandate: Mandate | None,
              only: list[str] | None = None) -> tuple[bool, str]:
        """Разрешить или отказать. Отказ обязан называть правило."""
        ...


@runtime_checkable
class MemoryTier(Protocol):
    """Память: очередь, факты, холодные артефакты, история."""

    def put(self, envelope: Envelope) -> bool: ...
    def get(self, envelope_id: str) -> dict[str, Any] | None: ...
    def inbox(self, agent: str, limit: int = 10) -> list[dict[str, Any]]: ...
    def claim(self, envelope_id: str, agent: str) -> tuple[dict[str, Any] | None, str]: ...
    def complete(self, envelope_id: str, result: dict[str, Any],
                 agent: str | None = None) -> bool: ...
    def pack(self, envelope: dict[str, Any]) -> dict[str, Any]: ...
    def remember(self, text: str, **kwargs: Any) -> int: ...
    def put_cold(self, text: str) -> str: ...
    def get_cold(self, ref: str) -> str | None: ...
    def history(self, envelope_id: str) -> list[dict[str, Any]]: ...
    def stats(self) -> dict[str, Any]: ...


@runtime_checkable
class Meter(Protocol):
    """Учёт по факту: сколько действие стоило."""

    name: str

    def quote(self, usage: dict[str, Any]) -> dict[str, Any]:
        """Вернуть цену по фактическому расходу."""
        ...


@runtime_checkable
class Anchor(Protocol):
    """Закрепление истории: запись и проверка целостности."""

    name: str

    def append(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def verify_chain(self) -> tuple[bool, str]: ...
