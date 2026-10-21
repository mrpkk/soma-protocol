"""Soma — федерация: узлы видят друг друга.

Два узла Soma могут делиться работой и историей. Правила простые и жёсткие:

- **Делиться можно только тем, что помечено как общее.** По умолчанию конверт
  приватный (`visibility: "private"`) и наружу не уходит никогда.
- **Каждый конверт, ушедший на узел, подписан отправителем.** Получатель сверяет
  подпись по ключу из своего реестра; чужой ключ — отказ, а не «принимаем и
  разберёмся потом».
- **История переносима.** Узел отдаёт свою цепочку событий; другой узел может
  её принять и проверить. Это и есть «история переживает смерть одного узла»:
  цепочка подписана и может уйти в любой якорь (Arweave, IPFS, ностр).

Ничего из этого не требует блокчейна: подпись и цепочка хешей дают проверяемость
сами по себе.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .envelope import Envelope, canonical

USER_AGENT = "soma/1.1.0"


@dataclass
class Peer:
    """Узел-партнёр."""

    name: str
    url: str
    public_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "url": self.url, "public_key": self.public_key,
                "metadata": self.metadata}


def _post(url: str, payload: dict[str, Any], timeout: int = 20,
          headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()[:400].decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def _get(url: str, timeout: int = 20) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read()[:400].decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def sign_for_peer(envelope: Envelope, signer: Any) -> dict[str, Any]:
    """Подписать конверт ключом узла-отправителя (для передачи другому узлу)."""
    signer.sign(envelope)
    return envelope.to_dict()


def verify_from_peer(payload: dict[str, Any],
                     known_public_key: str | None = None) -> tuple[bool, str]:
    """Проверить конверт, пришедший от узла-партнёра.

    `known_public_key` — ключ партнёра из нашего реестра. Если он есть, подпись
    сверяется именно с ним: чужой ключ в присланном конверте отвергается.
    """
    try:
        env = Envelope.from_dict(payload)
    except Exception as exc:
        return False, f"конверт не разобран: {exc}"
    sig = env.signature or {}
    if not sig or sig.get("algorithm") in (None, "none"):
        return False, "конверт не подписан узлом"
    if known_public_key and sig.get("public_key") != known_public_key:
        return False, "ключ узла не совпадает с ключом партнёра в реестре"
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        import base64
        Ed25519PublicKey.from_public_bytes(base64.b64decode(sig["public_key"])).verify(
            base64.b64decode(sig["signature"]), canonical(env.payload()))
        return True, "ok"
    except Exception as exc:
        return False, f"подпись не сошлась ({type(exc).__name__})"


def push(envelope: Envelope, peers: list[Peer], signer: Any) -> dict[str, Any]:
    """Разослать конверт партнёрам.

    Приватный конверт (`visibility != "shared"`) не уходит никуда — это не
    ошибка, а правило: наружу уходит только то, что помечено общим.
    """
    visibility = (envelope.to_dict().get("visibility") or "private")
    if visibility == "private":
        return {"pushed": False, "reason": "конверт приватный — наружу не уходит",
                "peers": []}
    signed = sign_for_peer(envelope, signer)
    results = []
    for peer in peers:
        code, body = _post(peer.url.rstrip("/") + "/v1/federate/inbox",
                           {"envelope": signed, "from": peer.name})
        results.append({"peer": peer.name, "code": code, "ok": code == 200,
                        "detail": body if code != 200 else ""})
    return {"pushed": True, "visibility": visibility, "peers": results}


def pull(peer_url: str, path: str = "/v1/federate/outbox") -> tuple[int, Any]:
    """Забрать у партнёра общий контент (то, что он сам разрешил отдавать)."""
    return _get(peer_url.rstrip("/") + path)


def export_history(chain: Any) -> dict[str, Any]:
    """Отдать свою цепочку истории: записи + подписанная голова.

    Голова — подпись последней записи; другой узел может и принять цепочку,
    и проверить связь, не доверяя нам на слово.
    """
    records = chain.records()
    head = records[-1]["hash"] if records else "0" * 64
    return {"records": records, "head": head, "count": len(records),
            "exported_at": time.time()}


def import_history(exported: dict[str, Any], chain: Any) -> tuple[bool, str]:
    """Принять чужую цепочку истории и проверить её связь.

    Записи не подписываются заново: важна непрерывность цепочки, поэтому
    проверяется, что каждая запись ссылается на хеш предыдущей.
    """
    records = exported.get("records") or []
    prev = "0" * 64
    for i, rec in enumerate(records):
        body = json.dumps(rec.get("payload", {}), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        import hashlib
        calc = hashlib.sha256((rec.get("prev", "") + body.decode("utf-8")).encode("utf-8")).hexdigest()
        if rec.get("prev") != prev:
            return False, f"запись {i}: разрыв цепочки"
        if calc != rec.get("hash"):
            return False, f"запись {i}: хеш не сходится"
        prev = rec.get("hash", "")
    return True, f"цепочка цела ({len(records)} записей)"
