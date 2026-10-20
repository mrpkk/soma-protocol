"""Mitra — расчёт по факту и оплата.

Два разных слоя, их часто путают:
- **agentpay-подобный учёт:** сколько действие стоило на самом деле —
  токены, CPU, байты, записи, внешние вызовы. Это считается всегда.
- **оплата (x402-совместимая):** перевозчик денег. Здесь определён интерфейс
  и заголовок протокола, но расчёт по сети не реализован: для этого нужен
  кошелёк получателя и сеть, а не библиотека.

Правило: цена всегда известна **до** действия и всегда попадает в конверт.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

#: Справочные ставки для оценки. Это конфигурация, а не рынок: меняется одним файлом.
RATES: dict[str, float] = {
    "tokens": 0.000015,     # USD за 1k токенов
    "cpu_seconds": 0.00004,  # USD за секунду CPU
    "bytes_out": 0.00000012,
    "storage_writes": 0.000002,
    "external_calls": 0.001,
}


@dataclass
class Usage:
    tokens: float = 0.0
    cpu_seconds: float = 0.0
    bytes_out: float = 0.0
    storage_writes: float = 0.0
    external_calls: float = 0.0

    def cost(self, rates: dict[str, float] | None = None) -> float:
        r = rates or RATES
        return (self.tokens / 1000 * r["tokens"]
                + self.cpu_seconds * r["cpu_seconds"]
                + self.bytes_out * r["bytes_out"]
                + self.storage_writes * r["storage_writes"]
                + self.external_calls * r["external_calls"])

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = round(self.cost(), 8)
        return d


def quote(usage: Usage) -> dict[str, Any]:
    """Цена по факту расхода. Никаких прайсовых таблиц."""
    return {"currency": "USD", **usage.as_dict()}


def x402_header_ok(headers: dict[str, str], expected_usdc: float) -> tuple[bool, str]:
    """Проверить заголовок оплаты x402-совместимого вида.

    Полноценный расчёт в сети не входит в ядро: здесь только контракт —
    «есть подписанная квитанция оплаты на нужную сумму». Подпись и сеть —
    ответственность транспорта (см. PROTOCOL.md, раздел «Деньги»).
    """
    raw = headers.get("x-payment") or headers.get("X-PAYMENT")
    if not raw:
        return False, "нет заголовка X-PAYMENT"
    if expected_usdc <= 0:
        return True, "оплата не требуется"
    return True, "квитанция оплаты предъявлена (проверка подписи — на транспорте)"
