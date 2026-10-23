"""Семантический канал: смысл без общих слов.

Зачем: FTS и основы слов находят «подпись» по «подписан», но не находят
«сколько стоит подписка» по «49 долларов в месяц» — общих слов нет. Эмбеддинги
закрывают ровно этот провал.

Правила, чтобы канал не притворялся:
- модель не обязательна: нет `fastembed` — канал просто выключен, recall работает;
- порог отсечки обязателен: ниже порога факт не считается найденным, иначе
  «не знаю» превращается в «вот что-то похожее»;
- векторы лежат в той же SQLite, что и факты: один файл, один бэкап, нет дрейфа.

Модель по умолчанию — multilingual MiniLM: русский и английский в одном
пространстве. Меняется переменной `SOMA_EMBED_MODEL`, порог — `SOMA_EMBED_THRESHOLD`.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_THRESHOLD = 0.35


class SemanticIndex:
    """Векторы фактов и поиск по смыслу. Деградирует молча, но честно."""

    def __init__(self, conn: sqlite3.Connection, model_name: str | None = None,
                 threshold: float | None = None):
        self.conn = conn
        self.model_name = model_name or os.getenv("SOMA_EMBED_MODEL", DEFAULT_MODEL)
        self.threshold = float(os.getenv("SOMA_EMBED_THRESHOLD", DEFAULT_THRESHOLD)
                               if threshold is None else threshold)
        self._model: Any = None
        self._lock = threading.Lock()
        self._backfilled = False
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fact_vectors("
            " fact_id INTEGER PRIMARY KEY, dim INTEGER, vec BLOB, model TEXT)")
        conn.commit()

    # ── доступность ────────────────────────────────────────────
    def available(self) -> bool:
        try:
            import fastembed  # noqa: F401
            return True
        except Exception:
            return False

    def _ensure_model(self):
        with self._lock:
            if self._model is None:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(self.model_name)
            return self._model

    @staticmethod
    def _normalized(vec) -> Any:
        import numpy as np
        v = np.asarray(vec, dtype="float32")
        n = float(np.linalg.norm(v))
        return v / n if n else v

    # ── запись ─────────────────────────────────────────────────
    def index(self, fact_id: int, text: str) -> bool:
        """Посчитать вектор факта. Ошибка модели не должна ломать запись факта."""
        if not self.available() or not (text or "").strip():
            return False
        try:
            v = self._normalized(next(iter(self._ensure_model().embed([text]))))
            self.conn.execute(
                "INSERT OR REPLACE INTO fact_vectors(fact_id, dim, vec, model) VALUES(?,?,?,?)",
                (int(fact_id), int(len(v)), v.tobytes(), self.model_name))
            self.conn.commit()
            return True
        except Exception:
            return False

    def index_missing(self) -> int:
        """Доиндексировать факты, которых нет в векторах (старая база)."""
        if not self.available():
            return 0
        rows = self.conn.execute(
            "SELECT f.id, f.text FROM facts f LEFT JOIN fact_vectors v ON v.fact_id = f.id"
            " WHERE f.status='active' AND v.fact_id IS NULL").fetchall()
        done = 0
        for fid, text in rows:
            if self.index(fid, text):
                done += 1
        return done

    # ── поиск ──────────────────────────────────────────────────
    def search(self, query: str, k: int = 20,
               threshold: float | None = None) -> list[tuple[int, float]]:
        """Вернуть (fact_id, сходство) выше порога. Пусто — значит пусто."""
        if not self.available() or not (query or "").strip():
            return []
        if not self._backfilled:
            self.index_missing()
            self._backfilled = True
        rows = self.conn.execute(
            "SELECT v.fact_id, v.vec FROM fact_vectors v JOIN facts f ON f.id = v.fact_id"
            " WHERE v.model=? AND f.status='active'", (self.model_name,)).fetchall()
        if not rows:
            return []
        try:
            import numpy as np
            q = self._normalized(next(iter(self._ensure_model().embed([query]))))
        except Exception:
            return []
        cut = self.threshold if threshold is None else threshold
        out: list[tuple[int, float]] = []
        for fid, blob in rows:
            v = np.frombuffer(blob, dtype="float32")
            if v.shape != q.shape:
                continue
            score = float(v @ q)
            if score >= cut:
                out.append((int(fid), score))
        out.sort(key=lambda x: -x[1])
        return out[:k]
