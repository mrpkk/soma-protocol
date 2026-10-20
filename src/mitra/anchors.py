"""Mitra — якоря истории.

История нужна, чтобы не наступать на одни и те же грабли: кто, когда, что
попросил, чем закончилось, какой отпечаток. В ядре — локальная цепочка
хешей (append-only, с отпечатком предыдущей записи). Внешние якоря
(Arweave, IPFS, Nostr) — адаптеры: интерфейс тот же, реализация — точка
расширения, потому что сеть здесь не нужна, чтобы доказать целостность.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Protocol


class Anchor(Protocol):
    """Якорь истории: записать и потом проверить, что запись не менялась."""

    def append(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def verify(self, record: dict[str, Any]) -> bool: ...


class LocalChain:
    """Append-only цепочка в файле. Хеш каждой записи включает хеш предыдущей."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else Path.home() / ".mitra" / "history.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return "0" * 64
        try:
            return json.loads(last).get("hash", "0" * 64)
        except Exception:
            return "0" * 64

    def append(self, payload: dict[str, Any]) -> dict[str, Any]:
        prev = self._last_hash()
        body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        h = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
        record = {"ts": time.time(), "prev": prev, "hash": h, "payload": payload}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return record

    def verify(self, record: dict[str, Any]) -> bool:
        prev = record.get("prev", "")
        body = json.dumps(record.get("payload", {}), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        h = hashlib.sha256((prev + body).encode("utf-8")).hexdigest()
        return h == record.get("hash")

    def verify_chain(self) -> tuple[bool, str]:
        """Проверить связь цепочкой: каждая запись ссылается на хеш предыдущей.

        Проверка одной записи не ловит подмену «в середине»: пересчитанный хеш
        проходит, если не смотреть связь. Поэтому нужна проверка цепочки.
        """
        recs = self.records()
        for i, rec in enumerate(recs):
            if not self.verify(rec):
                return False, f"запись {i}: хеш не сходится"
            if i and rec.get("prev") != recs[i - 1].get("hash"):
                return False, f"запись {i}: разрыв цепочки"
        return True, "цепочка цела"

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out
