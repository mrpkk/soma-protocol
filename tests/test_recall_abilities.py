"""Память: пять способностей, а не один recall@3.

По мотивам LongMemEval-V2 (MIT): извлечение, перенос между сессиями, время,
обновление факта и честное «не знаю». Провал здесь — это провал продукта,
поэтому проверяем каждую способность отдельно, а не средним баллом.

Запускать при любом изменении ранжирования: улучшение в одном месте
часто покупается провалом в другом.
"""
from __future__ import annotations

import time

import pytest

from soma.memory import Memory

BASE = [
    ("Soma costs 49 USD per month for one environment; one envelope costs 0.005 USDC.", "pricing"),
    ("Сома: одно окружение — 49 долларов в месяц; один конверт — 0.005 USDC.", "цены"),
    ("Every envelope is signed with Ed25519 and verification works offline.", "proofs"),
    ("Каждый конверт подписан Ed25519, проверка работает офлайн.", "доказательства"),
    ("Soma does not store prompts, model output, files or IP addresses.", "privacy"),
    ("Приватность: запросы, ответы моделей и IP-адреса не хранятся.", "приватность"),
]


def _mem(tmp_path) -> Memory:
    m = Memory(path=str(tmp_path / "m.db"))
    for text, tag in BASE:
        m.remember(text, tags=tag)
    return m


def _top(m: Memory, q: str, k: int = 3):
    return [r["text"] for r in m.recall(q, k=k)]


# ── 1. извлечение факта ────────────────────────────────────────────────
@pytest.mark.parametrize("q,expected", [
    ("how much does soma cost", "49 USD per month"),
    ("сколько стоит Сома", "49 долларов"),
    ("как проверяется подпись", "проверка работает офлайн"),
    ("does soma keep my prompts", "does not store prompts"),
])
def test_extraction(tmp_path, q, expected):
    assert any(expected in t for t in _top(_mem(tmp_path), q)), q


# ── 2. перенос между сессиями ──────────────────────────────────────────
def test_cross_session_recall(tmp_path):
    m = _mem(tmp_path)
    m.remember("Мы решили: цену держим 49 долларов, скидок не даём.", tags="решение", session="s1")
    m.remember("Во второй сессии проверяли федерацию двух узлов.", tags="работа", session="s2")
    hits = _top(m, "что решили про цену в прошлый раз")
    assert any("49 долларов" in t for t in hits), hits


# ── 3. время: старый факт не должен притворяться новым ─────────────────
def test_temporal_filter_returns_only_window(tmp_path):
    m = Memory(path=str(tmp_path / "m.db"))
    old_id = m.remember("Старая цена: 19 долларов в месяц.", tags="цены")
    m.conn.execute("UPDATE facts SET created_at=?, updated_at=? WHERE id=?",
                   (time.time() - 86400 * 30, time.time() - 86400 * 30, old_id))
    m.conn.commit()
    m.remember("Новая цена: 49 долларов в месяц.", tags="цены")
    since = time.time() - 86400
    hits = m.recall("цена в месяц", k=5, since=since)
    assert hits, "в окне последних суток факт обязан найтись"
    assert all("Старая цена" not in r["text"] for r in hits), [r["text"] for r in hits]


# ── 4. обновление: superseded не всплывает, новый факт первый ─────────
def test_superseded_fact_is_not_recalled(tmp_path):
    m = Memory(path=str(tmp_path / "m.db"))
    old = m.remember("Сома стоит 19 долларов в месяц.", tags="цены")
    m.supersede(old, "Сома стоит 49 долларов в месяц.", tags="цены")
    hits = _top(m, "сколько стоит сома")
    assert any("49 долларов" in t for t in hits), hits
    assert all("19 долларов" not in t for t in hits), hits


# ── 5. честное «не знаю»: нет факта — нет выдумки ─────────────────────
@pytest.mark.parametrize("q", [
    "какой у нас офис в Токио",
    "how many employees work in Berlin",
    "кто выиграл чемпионат по шахматам",
])
def test_abstention_returns_nothing_relevant(tmp_path, q):
    hits = _top(_mem(tmp_path), q)
    joined = " ".join(hits).lower()
    assert not any(w in joined for w in ("токио", "berlin", "шахмат")), hits
