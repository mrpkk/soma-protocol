"""Soma — репутация по фактам.

Репутация агента — не число лайков и не мнение. Это **подписанные квитанции**:
исполнитель подписывает результат своей работы, хаб проверяет подпись по ключу
из реестра и считает. Отказы по мандату тоже фиксируются — как нарушения.

Всё считается из проверяемых фактов, поэтому любой может пересчитатьScore
сам и не поверить нам на слово.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

#: Сколько стоит нарушение в весе: одно нарушение «съедает» четыре успеха.
VIOLATION_WEIGHT = 4
#: Порог доверия: столько проверенных квитанций — статус «established».
ESTABLISHED_AT = 10


def receipt_payload(agent: str, envelope_id: str, result_hash: str, ts: float | None = None) -> dict[str, Any]:
    """Тело квитанции: что именно подписывает исполнитель."""
    return {"agent": agent, "envelope_id": envelope_id, "result_hash": result_hash,
            "ts": ts if ts is not None else time.time()}


def canonical_receipt(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def sign_receipt(payload: dict[str, Any], private_bytes: bytes) -> dict[str, Any]:
    """Подписать квитанцию ключом исполнителя (Ed25519)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.from_private_bytes(private_bytes)
    sig = priv.sign(canonical_receipt(payload))
    return {
        "payload": payload,
        "algorithm": "ed25519",
        "signature": base64.b64encode(sig).decode(),
        "public_key": base64.b64encode(priv.public_key().public_bytes_raw()).decode(),
    }


def verify_receipt(receipt: dict[str, Any], expected_public_key: str | None = None) -> tuple[bool, str]:
    """Проверить квитанцию: подпись и (если известен) соответствие ключу реестра."""
    payload = receipt.get("payload")
    if not isinstance(payload, dict):
        return False, "в квитанции нет тела"
    if receipt.get("algorithm") != "ed25519":
        return False, f"неизвестный алгоритм: {receipt.get('algorithm')}"
    if expected_public_key and receipt.get("public_key") != expected_public_key:
        return False, "ключ квитанции не совпадает с ключом агента в реестре"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(
            base64.b64decode(receipt["public_key"])).verify(
            base64.b64decode(receipt["signature"]), canonical_receipt(payload))
        return True, "ok"
    except Exception as exc:
        return False, f"подпись не сошлась ({type(exc).__name__})"


def result_hash(result: Any) -> str:
    """Хеш результата — то, что стоит в квитанции."""
    body = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


class ReputationStore:
    """Счётчики по агентам: проверенные квитанции, нарушения, реестр нарушений.

    Хранится в JSON-файле: маленький, читаемый глазами, переносится вместе
    с узлом. При росте — переедет в БД, формат данных не изменится.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get(
            "SOMA_REPUTATION", Path.home() / ".soma" / "reputation.json"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}
        else:
            self.data = {}
        self.data.setdefault("receipts", {})
        self.data.setdefault("breaches", {})

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── квитанции ───────────────────────────────────────────
    def record_receipt(self, receipt: dict[str, Any],
                       expected_public_key: str | None = None) -> tuple[bool, str]:
        """Записать квитанцию, если подпись сходится. Неподписанное — не факт."""
        ok, why = verify_receipt(receipt, expected_public_key)
        if not ok:
            return False, why
        payload = receipt["payload"]
        agent = payload.get("agent", "unknown")
        env_id = payload.get("envelope_id", "")
        self.data["receipts"].setdefault(agent, {})[env_id] = {
            "result_hash": payload.get("result_hash", ""),
            "ts": payload.get("ts"),
            "public_key": receipt.get("public_key", ""),
        }
        self._save()
        return True, "ok"

    # ── нарушения ────────────────────────────────────────────
    def record_breach(self, agent: str, envelope_id: str, rule: str,
                      evidence: dict[str, Any] | None = None) -> None:
        """Фиксация нарушения: действие, отклонённое мандатом."""
        self.data["breaches"].setdefault(agent, []).append({
            "envelope_id": envelope_id, "rule": rule, "ts": time.time(),
            "evidence": evidence or {},
        })
        self._save()

    # ── счёт ─────────────────────────────────────────────────
    def score(self, agent: str) -> dict[str, Any]:
        """Репутация агента из проверенных фактов.

        Проверенные квитанции — вес 1, нарушение — вес VIOLATION_WEIGHT.
        `trust` — статус, а не число: unknown / observed / established.
        """
        receipts = self.data["receipts"].get(agent, {})
        breaches = self.data["breaches"].get(agent, [])
        completed = len(receipts)
        weight = completed - VIOLATION_WEIGHT * len(breaches)
        if completed == 0 and not breaches:
            trust = "unknown"
        elif completed >= ESTABLISHED_AT and not breaches:
            trust = "established"
        elif weight <= 0:
            trust = "breached"
        else:
            trust = "observed"
        return {
            "agent": agent,
            "verified_receipts": completed,
            "breaches": len(breaches),
            "weight": weight,
            "trust": trust,
            "last_receipt": max((r.get("ts") or 0) for r in receipts.values()) if receipts else None,
        }

    def agents(self) -> list[str]:
        return sorted(set(self.data["receipts"]) | set(self.data["breaches"]))
