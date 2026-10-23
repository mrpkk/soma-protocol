"""Бенчмарк памяти: recall@3 на двуязычном наборе.

Идея из Hindsight (MIT): память без измерения качества — это вера.
Набор маленький и честный: если ответ не в топ-3, это провал, а не «почти».
Запускать при каждом изменении ранжирования и раз в неделю в рыночном дозоре.
"""
from __future__ import annotations

import pytest

from soma.memory import Memory

FACTS = [
    ("Soma costs 49 USD per month for one environment; one envelope costs 0.005 USDC over x402.", "pricing"),
    ("Сома: одно окружение — 49 долларов в месяц; один конверт — 0.005 USDC.", "цены"),
    ("Every envelope is signed with Ed25519 and verification works offline.", "proofs"),
    ("Каждый конверт подписан Ed25519, проверка работает офлайн.", "доказательства"),
    ("Facts survive sessions and model changes; recall ranks by bm25.", "memory"),
    ("Факты переживают сессии и смену модели; поиск идёт по смыслу.", "память"),
    ("Soma does not store prompts, model output, files or IP addresses.", "privacy"),
    ("Приватность: запросы, ответы моделей и IP-адреса не хранятся.", "приватность"),
    ("Cold artifacts are fetched by sha256 and checked against the hash.", "cold"),
    ("Холодный ярус: артефакт достаётся по sha256 и сверяется с хешем.", "холодный-ярус"),
]

CASES = [
    ("how much does soma cost", "49 USD per month"),
    ("сколько стоит Сома", "49 долларов"),
    ("как проверяется подпись", "проверка работает офлайн"),
    ("offline signature verification", "verification works offline"),
    ("что хранится о приватности", "IP-адреса не хранятся"),
    ("does soma keep my prompts", "does not store prompts"),
    ("как достать холодный артефакт", "по sha256"),
    ("what happens to facts between sessions", "survive sessions"),
    ("переживают ли факты смену модели", "переживают сессии"),
    ("память и поиск", "поиск идёт по смыслу"),
]


def _memory(tmp_path) -> Memory:
    m = Memory(str(tmp_path / "q.db"), cold_dir=str(tmp_path / "cold"))
    for text, tags in FACTS:
        m.remember(text, tags=tags, importance=4)
    return m


@pytest.mark.parametrize("query,expected", CASES)
def test_recall_at_3(tmp_path, query, expected):
    m = _memory(tmp_path)
    rows = m.recall(query, k=3)
    assert rows, f"пустая выдача на «{query}»"
    texts = " ".join(r["text"] for r in rows).lower()
    assert expected.lower() in texts, f"«{query}»: в топ-3 нет {expected!r}"


def test_reflect_names_its_sources(tmp_path):
    m = _memory(tmp_path)
    out = m.reflect("сколько стоит Сома", k=3)
    assert out["count"] >= 1
    assert out["sources"] and out["observation_id"]
    edges = m.conn.execute("SELECT COUNT(*) c FROM edges WHERE relation='derived_from'").fetchone()["c"]
    assert edges == len(out["sources"])

CASES_PRECISE = [
    # Регрессия 24.10.2026: сущность с цифрой проигрывала соседней с тем же префиксом.
    ("Проект aurora-12: порт — 8185.", "какой порт у aurora-12", "8185"),
    ("Проект aurora-1: порт — 8000.", "какой порт у aurora-1", "8000"),
    ("Проект delta-77: кошелёк — 0xabc123.", "какой кошелёк у delta-77", "0xabc123"),
]


@pytest.mark.parametrize("fact,query,expected", CASES_PRECISE)
def test_recall_ranks_exact_entity_first(tmp_path, fact, query, expected):
    """Точное имя сущности должно стоять первым, а не «где-то в топ-3».

    Провал был не в поиске, а в ранжировании: FTS считал общим совпадением
    префикс «aurora-1» и «aurora-12», и нужный факт уходил ниже.
    """
    m = Memory(str(tmp_path / "p.db"), cold_dir=str(tmp_path / "cold"))
    m.remember(fact, tags="project", importance=3)
    m.remember("Проект aurora-121: порт — 9999.", tags="project", importance=3)
    m.remember("Проект aurora-13: порт — 7777.", tags="project", importance=3)
    rows = m.recall(query, k=3)
    assert rows, f"пустая выдача на «{query}»"
    assert expected in rows[0]["text"], f"«{query}»: первым пришёл {rows[0]['text']!r}"
