"""Mitra — память в три яруса: горячая, тёплая, холодная.

Правило, ради которого всё и делается: **контекст не растёт, растёт база.**
Запрос собирается под жёсткий бюджет токенов, и рядом возвращается честный
отчёт — что влезло, что не влезло. Модель получает «я не помню X» вместо
молчаливого обрезания.

- горячая: очередь конвертов (SQLite) — цель и задача всегда в контексте;
- тёплая: факты с версиями и связями (SQLite + FTS5, trigram) — по запросу;
- холодная: артефакты по sha256 — достаются по хешу, только если влезли.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

CHARS_PER_TOKEN = 220.0  # домашняя конвенция: ~220 символов на условный токен

SCHEMA = """
CREATE TABLE IF NOT EXISTS envelopes (
    id TEXT PRIMARY KEY, created_at REAL NOT NULL, ttl_s REAL NOT NULL,
    sender TEXT, recipient TEXT, goal TEXT, task TEXT, capability TEXT,
    token_budget INTEGER, context_query TEXT, cold_refs TEXT,
    mandate TEXT, price_usdc REAL, cost_usdc REAL, status TEXT, claimed_by TEXT,
    claimed_at REAL, result TEXT, signature TEXT, routing TEXT,
    assigned_to TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS ix_env ON envelopes(status, created_at);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, kind TEXT,
    tags TEXT, importance INTEGER, source TEXT, session TEXT,
    created_at REAL, updated_at REAL, status TEXT DEFAULT 'active',
    superseded_by INTEGER);

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(text, tags, tokenize='trigram');

CREATE TABLE IF NOT EXISTS edges (
    src INTEGER NOT NULL, relation TEXT NOT NULL, dst INTEGER NOT NULL,
    created_at REAL, PRIMARY KEY (src, relation, dst));

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, envelope_id TEXT,
    kind TEXT, payload TEXT);
"""

FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
  INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, tags) VALUES('delete', old.id, old.text, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
  INSERT INTO facts_fts(facts_fts, rowid, text, tags) VALUES('delete', old.id, old.text, old.tags);
  INSERT INTO facts_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
"""


class Memory:
    """Одно хранилище: очередь + факты + холодные артефакты."""

    def __init__(self, path: str | Path | None = None, cold_dir: str | Path | None = None,
                 chain: Any | None = None):
        self.path = Path(path or os.environ.get("SOMA_DB", Path.home() / ".soma" / "soma.db"))
        self.cold_dir = Path(cold_dir or os.environ.get("SOMA_COLD", Path.home() / ".soma" / "cold"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # HTTP-сервер многопоточный: соединение общее, доступ под локом.
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.lock = threading.RLock()
        from .anchors import LocalChain
        self.chain = chain if chain is not None else LocalChain(
            os.environ.get("SOMA_HISTORY", str(self.path.parent / "history.jsonl")))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.executescript(FTS_TRIGGERS)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Мягкая миграция: добавить недостающие колонки, не теряя данные."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(envelopes)").fetchall()}
        for name, ddl in (("assigned_to", "assigned_to TEXT DEFAULT ''"),
                          ("cost_usdc", "cost_usdc REAL")):
            if name not in cols:
                try:
                    self.conn.execute(f"ALTER TABLE envelopes ADD COLUMN {ddl}")
                except sqlite3.Error:
                    pass

    # ── очередь ───────────────────────────────────────────────
    def put(self, env: Any) -> bool:
        """Положить конверт в очередь. Существующий id не переигрывается."""
        with self.lock:
            return self._put(env)

    def _put(self, env: Any) -> bool:
        d = env.to_dict()
        d["status"] = "queued"   # статус определяет хаб, не отправитель
        try:
            self.conn.execute(
                "INSERT INTO envelopes(id, created_at, ttl_s, sender, recipient,"
            " goal, task, capability, token_budget, context_query, cold_refs, mandate,"
            " price_usdc, cost_usdc, status, claimed_by, claimed_at, result, signature, routing)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d["created_at"], d["ttl_s"], d["sender"], d.get("to", "*"), d["goal"],
             d["task"], json.dumps(d.get("capability", [])), d.get("token_budget", 2000),
             d.get("context_query", ""), json.dumps(d.get("cold_refs", [])),
             json.dumps(d.get("mandate") or {}), d.get("price_usdc"), d.get("cost_usdc"),
             d.get("status", "queued"),
             None, None, json.dumps(d.get("result")) if d.get("result") else None,
                 json.dumps(d.get("signature", {})), json.dumps(d.get("routing", {}))))
        except sqlite3.IntegrityError:
            # Лок держится, чужих незакоммиченных вставок не бывает:
            # откат откатывает только нашу транзакцию.
            self.conn.rollback()
            return False
        self.event(d["id"], "put", {"to": d.get("to")})
        self.conn.commit()
        return True

    def _row(self, env_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM envelopes WHERE id=?", (env_id,)).fetchone()

    def expire(self, now: float | None = None) -> int:
        with self.lock:
            now = now or time.time()
            rows = self.conn.execute(
                "SELECT id FROM envelopes WHERE status='queued' AND created_at + ttl_s < ?",
                (now,)).fetchall()
            n = 0
            for r in rows:
                self.conn.execute("UPDATE envelopes SET status='expired' WHERE id=?", (r["id"],))
                self.event(r["id"], "expired", {})
                n += 1
            self.conn.commit()
            return n

    def inbox(self, agent: str, limit: int = 10) -> list[dict[str, Any]]:
        with self.lock:
            self.expire()
            rows = self.conn.execute(
                "SELECT * FROM envelopes WHERE status='queued' AND (recipient=? OR recipient='*')"
                " ORDER BY created_at LIMIT ?", (agent, limit)).fetchall()
            return [self._to_env(r) for r in rows]

    def claim(self, env_id: str, agent: str) -> tuple[dict[str, Any] | None, str]:
        """Забрать конверт. Атомарно: второй претендент не получит.

        Забрать может только адресат (или broadcast) — иначе это кража работы.
        """
        with self.lock:
            self.expire()
            cur = self.conn.execute(
                "UPDATE envelopes SET status='claimed', claimed_by=?, claimed_at=?"
                " WHERE id=? AND status='queued' AND (recipient=? OR recipient='*')",
                (agent, time.time(), env_id, agent))
            self.conn.commit()
            if cur.rowcount == 0:
                row = self._row(env_id)
                if row is None:
                    return None, "нет такого конверта"
                if row["recipient"] not in (agent, "*"):
                    return None, f"конверт адресован {row['recipient']}"
                return None, f"конверт недоступен (status={row['status']})"
        if cur.rowcount == 0:
            row = self._row(env_id)
            return None, f"конверт недоступен (status={row['status'] if row else 'нет такого'})"
        self.event(env_id, "claimed", {"agent": agent})
        self.conn.commit()
        return self.get(env_id), "ok"

    def complete(self, env_id: str, result: dict[str, Any], agent: str | None = None) -> bool:
        """Закрыть конверт результатом. Чужой результат перезаписать нельзя."""
        with self.lock:
            row = self._row(env_id)
            if row is None:
                return False
            if agent and row["claimed_by"] and row["claimed_by"] != agent:
                return False
            cur = self.conn.execute(
                "UPDATE envelopes SET status='done', result=? WHERE id=? AND status IN ('claimed','queued')",
                (json.dumps(result, ensure_ascii=False, default=str), env_id))
            self.conn.commit()
            if cur.rowcount == 0:
                return False
            self.event(env_id, "completed", {"agent": agent})
            self.conn.commit()
            return True

    def get(self, env_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self._row(env_id)
            return self._to_env(row) if row else None

    def set_routing(self, env_id: str, routing: dict[str, Any]) -> None:
        """Записать решение роутера.

        Поле `to` — подписанное, его нельзя менять после подписи: иначе
        подпись перестаёт сходиться. Назначенный исполнитель живёт отдельно.
        """
        with self.lock:
            self.conn.execute(
                "UPDATE envelopes SET routing=?, assigned_to=COALESCE(NULLIF(?,''), assigned_to) WHERE id=?",
                (json.dumps(routing, ensure_ascii=False), routing.get("agent") or "", env_id))
            self.event(env_id, "routed", routing)
            self.conn.commit()

    @staticmethod
    def _to_env(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "created_at": row["created_at"], "ttl_s": row["ttl_s"],
            "sender": row["sender"], "to": row["recipient"], "goal": row["goal"],
            "task": row["task"], "capability": json.loads(row["capability"] or "[]"),
            "token_budget": row["token_budget"], "context_query": row["context_query"] or "",
            "cold_refs": json.loads(row["cold_refs"] or "[]"),
            "mandate": json.loads(row["mandate"] or "{}") or None,
            "price_usdc": row["price_usdc"], "cost_usdc": row["cost_usdc"],
            "status": row["status"],
            "claimed_by": row["claimed_by"], "claimed_at": row["claimed_at"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "signature": json.loads(row["signature"] or "{}"),
            "routing": json.loads(row["routing"] or "{}"),
            "assigned_to": row["assigned_to"] or "",
        }

    # ── факты (тёплый ярус) ───────────────────────────────────
    def remember(self, text: str, tags: str = "", importance: int = 3,
                 kind: str = "fact", source: str = "", session: str = "") -> int:
        text = (text or "").strip()
        tags = tags or ""
        if not text:
            raise ValueError("пустой факт не запоминается")
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO facts(text, kind, tags, importance, source, session,"
                    " created_at, updated_at, status) VALUES(?,?,?,?,?,?,?,?,'active')",
                (text, kind, tags.strip(), max(1, min(5, importance)), source, session, now, now))
            self.conn.commit()
            return int(cur.lastrowid)

    def supersede(self, old_id: int, new_text: str, **kw) -> int:
        new_id = self.remember(new_text, **kw)
        self.conn.execute("UPDATE facts SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                          (new_id, time.time(), old_id))
        self.conn.commit()
        return new_id

    def link(self, src: int, relation: str, dst: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO edges(src, relation, dst, created_at) VALUES(?,?,?,?)",
                          (src, relation, dst, time.time()))
        self.conn.commit()

    def recall(self, query: str, k: int = 20) -> list[dict[str, Any]]:
        """Найти факты: сначала полнотекстовый индекс (FTS5, trigram), при отказе — LIKE."""
        with self.lock:
            return self._recall(query, k)

    def _recall(self, query: str, k: int = 20) -> list[dict[str, Any]]:
        query = (query or "").strip()
        if not query:
            return []
        terms = [t for t in re.findall(r"[\wЀ-ӿ]+", query.lower()) if len(t) >= 3]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms[:6])
        try:
            rows = self.conn.execute(
                "SELECT f.* FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid"
                " WHERE facts_fts MATCH ? AND f.status='active'"
                " ORDER BY f.importance DESC, f.updated_at DESC LIMIT ?", (match, k)).fetchall()
            if rows:
                return [dict(r) for r in rows]
        except sqlite3.Error:
            pass  # индекс недоступен — падаем на LIKE ниже
        where = " OR ".join(f"text LIKE ?" for _ in terms[:6])
        rows = self.conn.execute(
            f"SELECT * FROM facts WHERE status='active' AND ({where}) ORDER BY importance DESC, updated_at DESC LIMIT ?",
            (*[f"%{t}%" for t in terms[:6]], k)).fetchall()
        return [dict(r) for r in rows]

    # ── холодный ярус ─────────────────────────────────────────
    def put_cold(self, text: str) -> str:
        self.cold_dir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        (self.cold_dir / f"{h}.txt").write_text(text, encoding="utf-8")
        return "sha256:" + h

    def get_cold(self, ref: str) -> str | None:
        """Достать артефакт по хешу.

        Ссылка — это sha256, а не имя файла: путь не собирается из ввода,
        и содержимое сверяется с хешем. Иначе «ссылка» была бы путём обхода.
        """
        h = ref.split(":", 1)[-1]
        if not re.fullmatch(r"[0-9a-f]{64}", h or ""):
            return None
        p = self.cold_dir / f"{h}.txt"
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8")
        if "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest() != ref:
            return None
        return text

    # ── сборка контекста под бюджет ──────────────────────────
    def pack(self, env: dict[str, Any]) -> dict[str, Any]:
        """Собрать контекст под жёсткий бюджет токенов конверта.

        Возвращает текст, потраченные токены, источники и — обязательно —
        список того, что не влезло. Молчаливого обрезания не бывает.
        """
        budget = int(env.get("token_budget") or 2000)
        approx = max(1.0, float(budget))
        used = 0.0
        parts: list[str] = []
        sources: list[str] = []
        dropped: list[str] = []

        # Горячий ярус (цель и задача) входит всегда: без него агент не знает,
        # что делать. Это единственное осознанное превышение бюджета.
        hot = f"ЦЕЛЬ: {env.get('goal','')}\nЗАДАЧА: {env.get('task','')}"
        cost = len(hot) / CHARS_PER_TOKEN
        parts.append(hot)
        used += cost
        sources.append("hot")
        if cost > approx:
            dropped.append("hot: цель и задача сами превысили бюджет")

        query = env.get("context_query") or ""
        if query:
            rows = self.recall(query, k=50)
            if not rows:
                dropped.append("warm: по запросу не найдено")
            else:
                for row in rows:
                    c = len(row["text"]) / CHARS_PER_TOKEN + 1
                    if used + c > approx:
                        dropped.append(f"warm: факт {row['id']} не влез в бюджет")
                        continue
                    parts.append(f"- {row['text']}")
                    used += c
                    sources.append("warm")

        for ref in env.get("cold_refs") or []:
            text = self.get_cold(ref)
            if text is None:
                dropped.append(f"cold:{ref}: артефакт не найден")
                continue
            c = len(text) / CHARS_PER_TOKEN + 1
            if used + c > approx:
                dropped.append(f"cold:{ref}: не влез в бюджет")
                continue
            parts.append(f"[{ref}] {text}")
            used += c
            sources.append("cold")

        return {"text": "\n\n".join(parts), "tokens": round(used), "budget": budget,
                "sources": sources, "dropped": dropped}

    # ── события (история) ─────────────────────────────────────
    def event(self, env_id: str | None, kind: str, payload: dict[str, Any]) -> None:
        """Записать событие: в таблицу и в хеш-цепочку истории."""
        with self.lock:
            body = {"env": env_id, "kind": kind, "payload": payload}
            self.conn.execute("INSERT INTO events(ts, envelope_id, kind, payload) VALUES(?,?,?,?)",
                              (time.time(), env_id, kind,
                               json.dumps(payload, ensure_ascii=False, default=str)))
            try:
                self.chain.append(body)
            except Exception:
                pass  # история не должна ломать работу хаба

    def history(self, env_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT ts, kind, payload FROM events WHERE envelope_id=? ORDER BY ts",
                                 (env_id,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        with self.lock:
            return self._stats()

    def _stats(self) -> dict[str, Any]:
        by_status = {r["status"]: (r["c"] or 0) for r in self.conn.execute(
            "SELECT status, COUNT(*) c FROM envelopes GROUP BY status").fetchall()}
        facts = self.conn.execute("SELECT COUNT(*) c FROM facts WHERE status='active'").fetchone()["c"]
        edges = self.conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        cold = len(list(self.cold_dir.glob("*.txt"))) if self.cold_dir.exists() else 0
        return {"envelopes": sum(by_status.values()), "by_status": by_status,
                "facts": facts, "edges": edges, "cold": cold,
                "db_bytes": self.path.stat().st_size if self.path.exists() else 0}
