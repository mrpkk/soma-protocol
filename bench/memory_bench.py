#!/usr/bin/env python3
"""Замер памяти Soma на длинной истории: recall@1/@3 против окна контекста.

Зачем: обещание «память переживает сессии и не деградирует» проверялось на
десяти фактах (tests/test_recall_quality.py). Десять фактов помещаются в любое
окно контекста — на них нельзя отличить память от везения.

Что измеряем на 1000 фактов:
  * soma — recall@1 и recall@3 (FTS bm25 + теги, слияние RRF);
  * окно — что увидит модель без памяти: последние W фактов истории;
  * полный контекст — все 1000 фактов (верхняя граница, но и цена в токенах).

Честные ограничения (важнее самих чисел):
  * корпус синтетический: сущности уникальны, формулировки короткие;
  * поиск лексический, не эмбеддинги: перефраз без общих слов провалится;
  * токены считаются оценкой len/4, а не настоящим токенизатором;
  * мерим извлечение, а не рассуждение по найденному.

Запуск: python3 bench/memory_bench.py [--facts 1000] [--queries 100] [--window 20]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from soma.memory import Memory  # noqa: E402

SUBJECTS = ["aurora", "borealis", "cascade", "delta", "ember", "fjord", "granite",
            "harbor", "iris", "juniper", "kestrel", "lagoon", "meridian", "nimbus",
            "onyx", "prairie", "quartz", "ridge", "summit", "tundra"]
ATTRIBUTES = [
    ("порт", lambda rng: str(rng.choice([8000, 8080, 8185, 8193, 9000]))),
    ("кошелёк", lambda rng: "0x" + "".join(rng.choice("0123456789abcdef") for _ in range(8))),
    ("владелец", lambda rng: rng.choice(["Аня", "Борис", "Вера", "Глеб", "Дина"])),
    ("версия", lambda rng: f"{rng.randint(1, 3)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}"),
    ("срок", lambda rng: f"{rng.randint(1, 30)} дней"),
    ("регион", lambda rng: rng.choice(["Мумбаи", "Бангалор", "Дели", "Пуна", "Ченнаи"])),
]


def build_corpus(n: int, seed: int = 7) -> tuple[list[tuple[str, str]], list[dict]]:
    """Факты + задания на поиск. Сущности уникальны: это худший случай для окон."""
    rng = random.Random(seed)
    facts: list[tuple[str, str]] = []
    probes: list[dict] = []
    used: set[str] = set()
    i = 0
    while len(facts) < n:
        i += 1
        subject = f"{rng.choice(SUBJECTS)}-{i}"
        attr, make = rng.choice(ATTRIBUTES)
        value = make(rng)
        text = f"Проект {subject}: {attr} — {value}."
        if text in used:
            continue
        used.add(text)
        facts.append((text, f"project,{attr}"))
        probes.append({"query": f"какой {attr} у {subject}", "answer": value,
                       "subject": subject, "index": len(facts) - 1})
    return facts, probes


def tokens(text: str) -> int:
    """Оценка токенов. Настоящий токенизатор в проекте не подключён — это допущение."""
    return max(1, len(text) // 4)


def evaluate(facts: list[tuple[str, str]], probes: list[dict], window: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        m = Memory(path=str(Path(tmp) / "bench.db"), cold_dir=str(Path(tmp) / "cold"))
        for text, tags in facts:
            m.remember(text, tags=tags, importance=3)

        soma_hits_1 = soma_hits_3 = soma_found = 0
        soma_tokens: list[int] = []
        window_hits = 0
        oracle_hits = 0
        misses: list[dict] = []

        for probe in probes:
            rows = m.recall(probe["query"], k=3)
            texts = [r["text"] for r in rows]
            soma_found += bool(texts)
            soma_tokens.append(sum(tokens(t) for t in texts))
            if texts and probe["answer"] in texts[0]:
                soma_hits_1 += 1
            if any(probe["answer"] in t for t in texts):
                soma_hits_3 += 1
            else:
                misses.append(probe)

            # Окно контекста: модель без памяти видит только последние W фактов.
            tail = facts[-window:]
            if any(probe["answer"] in t for t, _ in tail):
                window_hits += 1
            # Полный контекст: всё в промпте (верхняя граница качества).
            if any(probe["answer"] in t for t, _ in facts):
                oracle_hits += 1

        total = len(probes)
        return {
            "facts": len(facts),
            "queries": total,
            "window": window,
            "soma_hit1": soma_hits_1 / total,
            "soma_hit3": soma_hits_3 / total,
            "soma_empty": 1 - soma_found / total,
            "window_hit": window_hits / total,
            "oracle_hit": oracle_hits / total,
            "soma_tokens_median": statistics.median(soma_tokens),
            "window_tokens": tokens(" ".join(t for t, _ in facts[-window:])),
            "oracle_tokens": tokens(" ".join(t for t, _ in facts)),
            "misses": misses[:5],
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", type=int, default=1000)
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--window", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    facts, probes = build_corpus(args.facts)
    random.Random(11).shuffle(probes)
    probes = probes[: args.queries]
    result = evaluate(facts, probes, args.window)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0

    print(f"Фактов: {result['facts']} · запросов: {result['queries']} · окно: {result['window']}")
    print(f"soma recall@1: {result['soma_hit1']:.0%} · recall@3: {result['soma_hit3']:.0%} · пустая выдача: {result['soma_empty']:.0%}")
    print(f"окно контекста: {result['window_hit']:.0%} · полный контекст: {result['oracle_hit']:.0%}")
    print(f"токенов на ответ: soma ≈ {result['soma_tokens_median']:.0f} · окно ≈ {result['window_tokens']} · всё ≈ {result['oracle_tokens']}")
    if result["misses"]:
        print("примеры промахов:")
        for m in result["misses"]:
            print(f"  {m['query']} → ожидалось {m['answer']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
