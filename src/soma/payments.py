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

import json
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
        return (self.tokens / 1000 * r.get("tokens", 0.0)
                + self.cpu_seconds * r.get("cpu_seconds", 0.0)
                + self.bytes_out * r.get("bytes_out", 0.0)
                + self.storage_writes * r.get("storage_writes", 0.0)
                + self.external_calls * r.get("external_calls", 0.0))

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = round(self.cost(), 8)
        return d


class UsageMeter:
    """Эталонный шов учёта: цена по фактическому расходу."""

    name = "usage"

    def __init__(self, rates: dict[str, float] | None = None):
        self.rates = rates or dict(RATES)

    def quote(self, usage: dict[str, Any]) -> dict[str, Any]:
        u = Usage(**{k: v for k, v in (usage or {}).items()
                      if k in Usage.__dataclass_fields__})
        return {"currency": "USD", **u.as_dict()}


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
    try:
        receipt = json.loads(raw)
    except Exception:
        return False, "квитанция оплаты не является JSON"
    if not isinstance(receipt, dict):
        return False, "квитанция оплаты должна быть объектом"
    amount = receipt.get("amount")
    if amount is None:
        return False, "в квитанции нет суммы"
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return False, f"сумма в квитанции не число: {amount!r}"
    if amount + 1e-9 < expected_usdc:
        return False, f"сумма {amount} меньше требуемой {expected_usdc}"
    if receipt.get("asset") and not str(receipt["asset"]).upper().startswith("USDC"):
        return False, f"актив {receipt['asset']} не USDC"
    return True, "квитанция оплаты предъявлена (подпись и сеть — на транспорте)"
