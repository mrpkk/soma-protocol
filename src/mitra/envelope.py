"""Mitra — ядро протокола: конверт, мандат, роутер.

Одна мысль: сообщение между агентами — это **конверт**, а не текст в чате.
В конверте лежат цель, задача, бюджет, мандат, цена и подпись. Всё остальное
(память, транспорт, деньги, история) — перевозчики этого письма.

Ядро намеренно без зависимостей: только стандартная библиотека.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

VERSION = "1.0"
PROTOCOL = "mitra/1"

# ───────────────────────────────────────────────────────────── конверт

#: Канонизация перед подписью: ключи сортируются, разделители фиксированы.
def canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def digest(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(payload)).hexdigest()


@dataclass
class Mandate:
    """Что агенту разрешено. Проверяется **до** действия, а не после."""

    max_cost_usd: float | None = None
    max_calls: int | None = None
    allowed_payees: list[str] = field(default_factory=list)
    expires_at: float | None = None
    rules: list[str] = field(default_factory=list)  # имена правил для проверки

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], {})}


@dataclass
class Envelope:
    """Единое письмо между агентами."""

    goal: str
    task: str
    to: str = "*"                       # агент-получатель или "*" (broadcast)
    sender: str = "anonymous"
    capability: list[str] = field(default_factory=list)
    token_budget: int = 2000            # жёсткий бюджет контекста
    context_query: str = ""
    cold_refs: list[str] = field(default_factory=list)   # sha256-ссылки, не тела
    mandate: Mandate | None = None
    price_usdc: float | None = None      # максимум, что готов заплатить
    ttl_s: float = 3600.0
    id: str = ""
    created_at: float = 0.0
    signature: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    status: str = "queued"              # queued | claimed | done | expired | declined

    # ── создание и канонизация
    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:16]
        if not self.created_at:
            self.created_at = time.time()
        if isinstance(self.mandate, Mandate):
            self.mandate = self.mandate

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if isinstance(self.mandate, Mandate):
            d["mandate"] = self.mandate.as_dict()
        # Проводной формат: пустых ключей нет (None — это «не задано»).
        return {k: v for k, v in d.items() if v is not None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Envelope":
        d = dict(d)
        mand = d.pop("mandate", None)
        env = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        if mand:
            env.mandate = Mandate(**{k: v for k, v in mand.items()
                                     if k in Mandate.__dataclass_fields__})
        return env

    def payload(self) -> dict[str, Any]:
        """Что подписывается: всё, кроме изменяемых после подписи полей."""
        d = self.to_dict()
        for k in ("signature", "routing", "result", "status"):
            d.pop(k, None)
        return d

    def expired(self, now: float | None = None) -> bool:
        return time.time() >= (self.created_at + self.ttl_s)

    def budget_tokens(self) -> int:
        return max(1, int(self.token_budget))


# ───────────────────────────────────────────────────────────── подпись

def _ed25519_key() -> bytes | None:
    path = os.environ.get("MITRA_SIGNING_KEY")
    for candidate in (path, os.path.expanduser("~/.local/share/mitra/signing.key")):
        if candidate and os.path.exists(candidate):
            with open(candidate, "rb") as f:
                return f.read()
    return None



def generate_key(path: str | Path | None = None) -> dict[str, str]:
    """Создать пару Ed25519 для подписи конвертов. Приватный ключ — в файл."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    target = Path(path or os.environ.get(
        "MITRA_SIGNING_KEY", Path.home() / ".local" / "share" / "mitra" / "signing.key"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(f"ключ уже есть: {target} (старые подписи перестанут проверяться)")
    priv = Ed25519PrivateKey.generate()
    raw = priv.private_bytes_raw()
    # O_EXCL + 0600: файл создаётся сразу с правильными правами, без окна 0644.
    fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
    return {"private_key_path": str(target),
            "public_key": base64.b64encode(priv.public_key().public_bytes_raw()).decode()}

def sign(envelope: Envelope, key: bytes | None = None) -> Envelope:
    """Подписать конверт.

    Ed25519 (предпочтительно — публичный ключ едет вместе с подписью, проверка
    не требует ни сервиса, ни секрета). Если Ed25519 недоступен — HMAC-SHA256,
    который тоже можно проверить, но нужен общий секрет.
    """
    payload = canonical(envelope.payload())
    priv_bytes = key or _ed25519_key()
    if priv_bytes is not None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        priv = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        sig = priv.sign(payload)
        pub = priv.public_key().public_bytes_raw()
        envelope.signature = {
            "algorithm": "ed25519",
            "signature": base64.b64encode(sig).decode(),
            "public_key": base64.b64encode(pub).decode(),
            "signer": "hub",
        }
        return envelope
    secret = os.environ.get("MITRA_SECRET")
    if not secret:
        # Молча подписывать «ничем» нельзя: это выглядит как подпись, а значит —
        # как доверие. Явная ошибка лучше тишины.
        raise RuntimeError(
            "нечем подписать: нет ключа Ed25519 (MITRA_SIGNING_KEY) и нет MITRA_SECRET")
    mac = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    envelope.signature = {"algorithm": "hmac-sha256", "signature": mac, "signer": "hub"}
    return envelope


def verify(envelope: Envelope, secret: bytes | None = None,
           expected_public_key: str | None = None) -> tuple[bool, str]:
    """Проверить подпись конверта. Возвращает (ok, причина).

    `expected_public_key` — ключ из реестра агентов. С ним проверка доказывает
    **происхождение**: без него доказывается только целостность, потому что
    публичный ключ едет в самом конверте.
    """
    sig = envelope.signature or {}
    algo = sig.get("algorithm")
    if not algo or algo == "none":
        return False, "конверт не подписан"
    payload = canonical(envelope.payload())
    if algo == "hmac-sha256":
        sec = secret or (os.environ.get("MITRA_SECRET") or "").encode()
        if not sec:
            return False, "нет общего секрета для проверки HMAC"
        expect = hmac.new(sec, payload, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(expect, sig.get("signature", ""))
        return ok, "ok" if ok else "подпись не сошлась"
    if algo == "ed25519":
        if expected_public_key and sig.get("signer") == "client" \
            and sig.get("public_key") != expected_public_key:
            return False, "ключ не совпадает с ключом отправителя из реестра"
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey,
            )
            Ed25519PublicKey.from_public_bytes(
                base64.b64decode(sig["public_key"])).verify(
                base64.b64decode(sig["signature"]), payload)
            return True, "ok"
        except Exception as exc:
            return False, f"подпись не сошлась ({type(exc).__name__})"
    return False, f"неизвестный алгоритм: {algo}"


# ───────────────────────────────────────────────────────────── мандат

#: Правила по умолчанию. Порядок фиксирован: отказ называет самую важную причину,
#: а не попутную. Совместимо с политиками вида OPA/Cedar: правило = (id, проверка).
RULES: list[tuple[str, Callable[[dict, Mandate], str | None]]] = [
    ("validity_window", lambda a, m: None if (m.expires_at is None or a.get("at", time.time()) <= m.expires_at)
     else "срок мандата истёк"),
    ("amount", lambda a, m: None if m.max_cost_usd is None else (
         None if "amount" in a and _num(a["amount"]) <= m.max_cost_usd
         else (f"в действии нет суммы" if "amount" not in a
               else f"сумма {a['amount']} выше мандата {m.max_cost_usd}"))),
    ("payee", lambda a, m: None if not m.allowed_payees else (
         None if "payee" in a and a["payee"] in m.allowed_payees
         else ("в действии нет получателя" if "payee" not in a
               else f"получатель {a.get('payee')} не в списке разрешённых"))),
    ("calls", lambda a, m: None if m.max_calls is None else (
         None if "calls" in a and _num(a["calls"]) <= m.max_calls
         else ("в действии нет числа вызовов" if "calls" not in a
               else f"число вызовов {a.get('calls')} выше мандата {m.max_calls}"))),
]


def _num(value: Any) -> float:
    """Число или исключение: нечисловое значение — это ошибка, а не ноль."""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"значение не число: {value!r}")


def check_mandate(action: dict[str, Any], mandate: Mandate | None,
                  only: list[str] | None = None) -> tuple[bool, str]:
    """Проверить действие против мандата. Отказ — всегда с именем правила.

    Отказ по умолчанию: неизвестное имя правила, отсутствующее поле при
    включённом правиле и нечисловое значение — это отказ, а не «разрешено».
    """
    if mandate is None:
        return True, "ok"
    known = {r[0] for r in RULES}
    unknown = set(mandate.rules or []) - known
    if unknown:
        return False, f"unknown_rule: {sorted(unknown)}"
    enabled = set(mandate.rules) if mandate.rules else known
    if only is not None:
        # Проверяем только указанные правила: «сумма» относится к оплате,
        # а не к выдаче работы, и не должна блокировать её.
        enabled &= set(only)
    for rule_id, fn in RULES:
        if rule_id not in enabled:
            continue
        try:
            reason = fn(action, mandate)
        except ValueError as exc:
            return False, f"{rule_id}: {exc}"
        if reason:
            return False, f"{rule_id}: {reason}"
    return True, "ok"


# ───────────────────────────────────────────────────────────── роутер

def route(envelope: Envelope, registry: dict[str, Any]) -> dict[str, Any]:
    """Выбрать исполнителя: capability ∩ мандат ∩ цена.

    Нет кандидата — честный отказ с причинами по каждому отброшенному агенту.
    Молча терять задачу нельзя: это и есть «тихая потеря» в агентных системах.
    """
    caps = set(envelope.capability or [])
    mand = envelope.mandate
    cands: list[tuple[float, str, dict]] = []
    rejected: list[str] = []
    for name, spec in (registry or {}).items():
        agent_caps = set(spec.get("capabilities") or [])
        if caps and not (caps & agent_caps):
            rejected.append(f"{name}: не умеет {sorted(caps)} (умеет {sorted(agent_caps)})")
            continue
        cost = float(spec.get("cost_per_call", 0) or 0)
        if mand and mand.max_cost_usd is not None and cost > mand.max_cost_usd:
            rejected.append(f"{name}: цена {cost} выше мандата {mand.max_cost_usd}")
            continue
        if envelope.price_usdc is not None and cost > float(envelope.price_usdc):
            rejected.append(f"{name}: цена {cost} выше price_usdc {envelope.price_usdc}")
            continue
        if mand and mand.allowed_payees and envelope.to not in mand.allowed_payees and envelope.to != "*":
            rejected.append(f"{name}: получатель {envelope.to} не разрешён мандатом")
            continue
        cands.append((cost, name, spec))
    if not cands:
        return {"agent": None, "reason": "нет агента, проходящего capability+мандат+цену",
                "rejected": rejected}
    cands.sort(key=lambda x: (x[0], x[1]))
    cost, name, _ = cands[0]
    return {"agent": name, "reason": f"подходит по capability и цене (cost_per_call={cost})",
            "cost_per_call": cost, "rejected": rejected,
            "alternatives": [c[1] for c in cands[1:]]}


# ───────────────────────────────────────────────────────────── схема

_ENVELOPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://mitra.dev/schemas/envelope-1.0.json",
    "title": "Mitra envelope",
    "type": "object",
    "required": ["goal", "task", "id", "created_at"],
    "properties": {
        "id": {"type": "string", "minLength": 4, "maxLength": 64},
        "created_at": {"type": "number"},
        "ttl_s": {"type": "number", "exclusiveMinimum": 0},
        "goal": {"type": "string", "minLength": 1, "maxLength": 2000},
        "task": {"type": "string", "minLength": 1, "maxLength": 20000},
        "sender": {"type": "string"},
        "to": {"type": "string"},
        "capability": {"type": "array", "items": {"type": "string"}},
        "token_budget": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "context_query": {"type": "string"},
        "cold_refs": {"type": "array", "items": {"type": "string"}},
        "mandate": {
            "type": "object",
            "properties": {
                "max_cost_usd": {"type": "number", "minimum": 0},
                "max_calls": {"type": "integer", "minimum": 1},
                "allowed_payees": {"type": "array", "items": {"type": "string"}},
                "expires_at": {"type": "number"},
                "rules": {"type": "array", "items": {"type": "string"}},
            },
        },
        "price_usdc": {"type": "number", "minimum": 0},
        "status": {"type": "string",
                   "enum": ["queued", "claimed", "done", "expired", "declined"]},
        "signature": {"type": "object"},
        "routing": {"type": "object"},
        "result": {"type": ["object", "null"]},
    },
}


def validate(envelope: Envelope) -> list[str]:
    """Проверить конверт по схеме. Пустой список = валиден.

    `jsonschema` используется, если установлен; иначе — встроенная проверка
    обязательных полей и типов. Протокол не должен требовать зависимость.
    """
    d = envelope.to_dict()
    try:
        import jsonschema  # type: ignore
        errors = sorted(jsonschema.Draft202012Validator(_ENVELOPE_SCHEMA).iter_errors(d),
                        key=lambda e: e.path)
        return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
    except ImportError:
        # jsonschema не установлен — проверяем сами, строго: типы, границы, enum.
        errors: list[str] = []
        for field in _ENVELOPE_SCHEMA["required"]:
            if not d.get(field):
                errors.append(f"{field}: обязательное поле")
        for field, typ in (("goal", str), ("task", str), ("sender", str), ("to", str),
                           ("context_query", str), ("id", str)):
            if field in d and not isinstance(d[field], typ):
                errors.append(f"{field}: должен быть {typ.__name__}")
        for field in ("created_at", "ttl_s"):
            if field in d and not isinstance(d[field], (int, float)) or isinstance(d.get(field), bool):
                errors.append(f"{field}: должен быть числом")
        if "ttl_s" in d and isinstance(d["ttl_s"], (int, float)) and d["ttl_s"] <= 0:
            errors.append("ttl_s: должен быть > 0")
        if "token_budget" in d:
            if not isinstance(d["token_budget"], int) or isinstance(d["token_budget"], bool):
                errors.append("token_budget: должен быть целым")
            elif d["token_budget"] < 1:
                errors.append("token_budget: должен быть >= 1")
        if "capability" in d:
            if not isinstance(d["capability"], list) or \
                    any(not isinstance(x, str) for x in d["capability"]):
                errors.append("capability: должен быть списком строк")
        if "cold_refs" in d and (not isinstance(d["cold_refs"], list) or
                                 any(not isinstance(x, str) for x in d["cold_refs"])):
            errors.append("cold_refs: должен быть списком строк")
        if "price_usdc" in d and (not isinstance(d["price_usdc"], (int, float)) or
                                  isinstance(d["price_usdc"], bool)):
            errors.append("price_usdc: должен быть числом")
        if "status" in d and d["status"] not in _ENVELOPE_SCHEMA["properties"]["status"]["enum"]:
            errors.append("status: неизвестное значение")
        if "mandate" in d and not isinstance(d["mandate"], dict):
            errors.append("mandate: должен быть объектом")
        return errors
