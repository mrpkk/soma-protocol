"""Soma — адаптер MCP.

Тот же протокол, другой перевозчик. Конверт не меняется: MCP-клиент видит
инструменты, которые кладут и забирают конверты из очереди хаба.

    python3 -m soma.mcp_adapter            # stdio (по умолчанию)
    python3 -m soma.mcp_adapter --http 8765

Требуется mcp SDK (`pip install "soma-protocol[mcp]"`). Без него ядро работает
и без MCP — это осознанно: протокол не должен зависеть от перевозчика.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # pragma: no cover
    print("нужен mcp SDK: pip install \"soma-protocol[mcp]\"", file=sys.stderr)
    raise

from .envelope import Envelope, Mandate, verify
from .memory import Memory
from .registry import Registry
from .server import Hub

mcp = MCPServer("soma", version="1.0.0")
_HUB: Hub | None = None


def hub() -> Hub:
    global _HUB
    if _HUB is None:
        _HUB = Hub(Memory(), Registry())
    return _HUB


def _envelope(**kwargs) -> str:
    mand = kwargs.pop("mandate", None)
    env = Envelope(**kwargs)
    if mand:
        env.mandate = Mandate(**{k: v for k, v in mand.items()
                                 if k in Mandate.__dataclass_fields__})
    return json.dumps(hub().send(env), ensure_ascii=False)


#: Публичный узел не хранит ничего приватного: видимость задаёт сервер.
#: Читается в момент вызова, а не при импорте — иначе смена режима не подхватывается.
def _forced_visibility() -> str:
    return os.environ.get("SOMA_FORCE_VISIBILITY", "")


@mcp.tool(name="soma_send",
          description="Отправить конверт агенту: цель, задача, получатель, бюджет токенов, мандат и цена. Конверт подписывается и попадает в очередь.")
async def soma_send(goal: str, task: str, to: str = "*", sender: str = "mcp",
                     capability: list[str] | None = None, token_budget: int = 2000,
                     context_query: str = "", max_cost_usd: float | None = None,
                     price_usdc: float | None = None, cost_usdc: float | None = None,
                     ttl_s: float = 3600,
                     visibility: str = "private") -> str:
    mand = {"max_cost_usd": max_cost_usd} if max_cost_usd is not None else None
    vis = _forced_visibility() or visibility
    return _envelope(goal=goal, task=task, to=to, sender=sender,
                     capability=capability or [], token_budget=token_budget,
                     context_query=context_query, mandate=mand,
                     price_usdc=price_usdc, cost_usdc=cost_usdc,
                     ttl_s=ttl_s, visibility=vis)


@mcp.tool(name="soma_inbox",
          description="Что ждёт агента: конверты, проверка подписи и контекст, собранный под бюджет токенов.")
async def soma_inbox(agent: str, limit: int = 10) -> str:
    return json.dumps(hub().inbox(agent, limit), ensure_ascii=False, default=str)


@mcp.tool(name="soma_claim",
          description="Забрать конверт из очереди (эксклюзивно) и получить контекст под бюджет. Если у работы назначена цена — нужна квитанция.")
async def soma_claim(id: str, agent: str, payment: str = "") -> str:
    return json.dumps(hub().claim(id, agent, payment=payment),
                      ensure_ascii=False, default=str)


@mcp.tool(name="soma_complete",
          description="Закрыть конверт результатом работы.")
async def soma_complete(id: str, result: str, agent: str = "") -> str:
    return json.dumps(hub().complete(id, {"text": result}, agent or None), ensure_ascii=False)


@mcp.tool(name="soma_route",
          description="Подобрать исполнителя по capability, мандату и цене. Если кандидата нет — вернётся честная причина отказа.")
async def soma_route(id: str) -> str:
    return json.dumps(hub().route(id), ensure_ascii=False, indent=2)


@mcp.tool(name="soma_verify",
          description="Проверить подпись конверта офлайн: Ed25519, публичный ключ едет вместе с подписью.")
async def soma_verify(envelope_json: str) -> str:
    env = Envelope.from_dict(json.loads(envelope_json))
    ok, why = verify(env)
    return json.dumps({"ok": ok, "reason": why, "algorithm": (env.signature or {}).get("algorithm")},
                      ensure_ascii=False)


@mcp.tool(name="soma_stats",
          description="Состояние хаба: конверты по статусам, факты, связи, холодные артефакты.")
async def soma_stats() -> str:
    return json.dumps(hub().memory.stats(), ensure_ascii=False, indent=2)


# ── память: то, ради чего агент возвращается ────────────────────────────────
# Без этих четырёх инструментов хаб был только очередью поручений: агент
# приходил, закрывал конверт и уходил ни с чем. Память живёт в том же SQLite,
# что и очередь, поэтому факты и обязательства видны вместе.

@mcp.tool(name="soma_remember",
          description="Запомнить факт: переживает сессии и смену модели. Теги для поиска, importance 1–5.")
async def soma_remember(text: str, tags: str = "", importance: int = 3,
                        kind: str = "fact", source: str = "", session: str = "") -> str:
    fid = hub().memory.remember(text, tags=tags, importance=importance,
                                kind=kind, source=source, session=session)
    return json.dumps({"id": fid, "stored": True}, ensure_ascii=False)


@mcp.tool(name="soma_recall",
          description="Найти факты: полнотекстовый поиск по смыслу, ранжирование по важности и свежести.")
async def soma_recall(query: str, k: int = 10) -> str:
    rows = hub().memory.recall(query, k=k)
    return json.dumps({"query": query, "count": len(rows), "facts": rows},
                      ensure_ascii=False, default=str)


@mcp.tool(name="soma_export",
          description="Выгрузить все факты и конверты узла одним JSON — право выхода без замка и без удаления за вами.")
async def soma_export(limit: int = 5000) -> str:
    h = hub()
    with h.memory.lock:
        facts = [dict(r) for r in h.memory.conn.execute(
            "SELECT id,text,tags,importance,kind,source,session,created_at,updated_at,status"
            " FROM facts ORDER BY id LIMIT ?", (limit,)).fetchall()]
        envs = [dict(r) for r in h.memory.conn.execute(
            "SELECT * FROM envelopes ORDER BY created_at LIMIT ?", (limit,)).fetchall()]
    return json.dumps({"exported_at": time.time(),
                       "counts": {"facts": len(facts), "envelopes": len(envs)},
                       "facts": facts, "envelopes": envs},
                      ensure_ascii=False, default=str)


@mcp.tool(name="soma_reflect",
          description="Собрать наблюдение по запросу: связка фактов с источниками и рёбрами derived_from. Ответ указывает, из чего выведен.")
async def soma_reflect(query: str, k: int = 8, save: bool = True) -> str:
    return json.dumps(hub().memory.reflect(query, k=k, save=save),
                      ensure_ascii=False, default=str)


@mcp.tool(name="soma_cold_put",
          description="Положить большой артефакт в холодный ярус. Возвращает ссылку sha256 — тело не жжёт контекст.")
async def soma_cold_put(text: str) -> str:
    return json.dumps({"ref": hub().memory.put_cold(text), "stored": True}, ensure_ascii=False)


@mcp.tool(name="soma_cold_get",
          description="Достать артефакт по ссылке sha256; содержимое сверяется с хешем.")
async def soma_cold_get(ref: str) -> str:
    text = hub().memory.get_cold(ref)
    ok = text is not None
    return json.dumps({"ref": ref, "found": ok, "text": text or ""}, ensure_ascii=False)



_PRIVACY = '# Privacy — soma\n\nsoma is a protocol for obligations between agents: who promised what, to whom,\nby when. That sentence is the privacy policy.\n\n## What is stored\n\n- **Envelopes** — the promise itself: sender, recipient, what was promised, the\n  deadline, the status. This is the product: an obligation you cannot store\n  cannot be kept.\n- **Request counters** — how many calls per tool per day.\n- **The rate limiter** keeps your client address in memory for one hour. It is\n  not written to disk.\n\n## What is not stored\n\n- your prompts, your model output, your files;\n- anything about your machine;\n- IP addresses in persistent form.\n\n## Who can read an envelope\n\nThe envelope exists so that the other side can act on it. It is readable by the\nsender, the recipient and the node that routes it. Do not put secrets in an\nenvelope: it is a record of an obligation, not a vault.\n\n## Deletion and questions\n\nOpen an issue at https://github.com/mrpkk/soma-protocol/issues. Envelopes are\nremoved on request; counters are not personal and are kept as totals.\n'

_LLMS_INSTALL = '# soma — install\n\nRemote MCP server (streamable HTTP). No key needed for the read-only demo.\n\n```json\n{"mcpServers": {"soma": {"url": "https://rta.lat/hub/mcp",\n  "headers": {"Authorization": "Bearer $SOMA_TOKEN"}}}}\n```\n\nMemory: `soma_remember` — store a fact, `soma_recall` — search by meaning,\n`soma_cold_put` / `soma_cold_get` — large artifacts by sha256.\n\nObligations: `soma_send` — promise work (goal, task, recipient, token budget,\nmandate, price), `soma_inbox` — what waits for the agent, `soma_claim` — take it\nexclusively, `soma_complete` — close with a signed receipt, `soma_route` — pick an\nexecutor by capability and price, `soma_verify` — check a signature offline,\n`soma_stats` — node state.\n\nEndpoint: `https://rta.lat/hub/mcp` · no token: `GET https://rta.lat/hub/status` ·\nPrivacy: `https://rta.lat/hub/privacy` · Source: https://github.com/mrpkk/soma-protocol\n'

#: Что умеет узел — один список на лендинг, llms-install и документацию.
_TOOLS = [
    ("soma_remember", "Store a fact. Survives sessions and model changes; tags and importance 1–5."),
    ("soma_recall", "Search facts by meaning: full-text index, ranked by importance and recency."),
    ("soma_send", "Promise work: goal, task, recipient, token budget, mandate, price. Signed and queued."),
    ("soma_inbox", "What waits for the agent: envelopes with verified signatures and packed context."),
    ("soma_claim", "Take an envelope exclusively; get context packed under its token budget."),
    ("soma_complete", "Close an envelope with a result. The receipt is signed and verifiable offline."),
    ("soma_route", "Pick an executor by capability, mandate and price. A refusal names every reason."),
    ("soma_verify", "Verify an envelope signature offline: Ed25519, the key travels with the signature."),
    ("soma_cold_put", "Put a large artifact in the cold tier; get a sha256 reference instead of a body."),
    ("soma_cold_get", "Fetch a cold artifact by sha256; content is checked against the hash."),
    ("soma_stats", "Node state: envelopes by status, facts, edges, cold artifacts, database size."),
    ("soma_reflect", "Form an observation from related facts; the answer names its sources (derived_from edges)."),
    ("soma_export", "Export every fact and envelope as one JSON — the right to leave, with no lock-in."),
]

_CSS = """
:root{--bg:#0b0d12;--panel:#141821;--line:#222838;--fg:#e8ecf4;--dim:#969eac;--acc:#f0a35e;--ok:#7dd3a0}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);font:16px/1.65 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;
     margin:0;padding:0 24px 64px}
main{max-width:880px;margin:0 auto}
header{padding:56px 0 8px}
.badge{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:4px 12px;
       color:var(--dim);font-size:13px;letter-spacing:.02em}
h1{font-size:40px;line-height:1.1;margin:18px 0 10px;letter-spacing:-.02em}
h1 span{color:var(--acc)}
.lead{color:var(--dim);font-size:18px;max-width:640px;margin:0 0 22px}
.lead b{color:var(--fg)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:18px 0 34px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card h3{margin:0 0 6px;font-size:16px}
.card p{margin:0;color:var(--dim);font-size:14px}
.stats{display:flex;gap:26px;flex-wrap:wrap;background:var(--panel);border:1px solid var(--line);
       border-radius:12px;padding:14px 18px;margin:0 0 30px;font-size:14px}
.stats div{color:var(--dim)} .stats b{color:var(--fg);font-size:18px;display:block;line-height:1.2}
h2{font-size:22px;margin:34px 0 10px;letter-spacing:-.01em}
pre,code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px;
    overflow-x:auto;font-size:13.5px;color:#cfd6e4}
code{background:#1b2130;border-radius:5px;padding:1px 6px;font-size:13.5px;color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:14px;margin:8px 0 0}
td{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top}
td:first-child{white-space:nowrap;color:var(--acc);font-family:ui-monospace,monospace;font-size:13px}
td:last-child{color:var(--dim)}
a{color:var(--acc);text-decoration:none} a:hover{text-decoration:underline}
.ask{display:flex;gap:8px;margin:0 0 10px}
.ask input{flex:1;background:var(--panel);border:1px solid var(--line);border-radius:9px;
           padding:11px 14px;color:var(--fg);font-size:15px;outline:none}
.ask input:focus{border-color:var(--acc)}
.ask button{background:var(--acc);color:#131313;border:0;border-radius:9px;padding:11px 18px;
            font-size:15px;font-weight:600;cursor:pointer}
.ask button:hover{filter:brightness(1.07)}
footer{margin-top:40px;padding-top:18px;border-top:1px solid var(--line);color:var(--dim);font-size:14px}
.pill{color:var(--ok)}
"""


#: Скрипт демо вынесен из f-строки: фигурные скобки JS в шаблоне — синтаксическая ошибка.
_DEMO_JS = """
function ask(){
  var q=(document.getElementById('q').value||'').trim();
  var out=document.getElementById('out');
  if(q.length<2){out.textContent='Type at least two characters.';return;}
  out.textContent='searching\u2026';
  fetch('/hub/demo/recall?q='+encodeURIComponent(q))
    .then(function(r){return r.json();})
    .then(function(d){
      if(d.error){out.textContent=d.error;return;}
      if(!d.count){
        out.textContent='Nothing found for "'+q+'". The node has '+out.dataset.facts+' facts.';
        return;
      }
      out.textContent=d.facts.map(function(f){
        return '\u2022 '+f.text+(f.tags?'  ['+f.tags+']':'');
      }).join('\\n');
    })
    .catch(function(){out.textContent='Request failed. Try again in a moment.';});
}
document.getElementById('q').addEventListener('keydown',function(e){if(e.key==='Enter')ask();});
"""

def _landing(stats: dict[str, object]) -> str:
    """Публичная страница узла: что это, зачем и как подключиться за минуту.

    Без токена — потому что первое, что делает человек или агент, — смотрит,
    что здесь есть. 401 на главной отпугивал и его, и краулер каталогов MCP.
    """
    rows = "".join(f"<tr><td>{n}</td><td>{d}</td></tr>" for n, d in _TOOLS)
    by_status = stats.get("by_status") or {}
    live = 0
    if isinstance(by_status, dict):
        live = int(by_status.get("queued", 0) or 0) + int(by_status.get("claimed", 0) or 0)
        done = int(by_status.get("done", 0) or 0)
    else:
        done = 0
    return f"""<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Soma — memory, obligations and proofs for agents</title>
<meta name=description content="One MCP endpoint that gives an agent long memory, signed obligations and offline proofs. Open protocol, free tier, no account.">
<style>{_CSS}</style>
<main>
<header>
  <span class=badge>MIT protocol · free tier · no account</span>
  <h1>Soma<span>.</span> memory, obligations, proofs</h1>
  <p class=lead>One MCP endpoint for agents that must <b>remember</b>, <b>promise</b> and
  <b>prove</b>. Facts outlive the session. Work is sent, claimed and closed with a
  signature anyone can check offline. No SDK, no card, no vendor lock.</p>
</header>

<div class=stats>
  <div><b>{stats.get('facts', 0)}</b>facts remembered</div>
  <div><b>{live}</b>open obligations</div>
  <div><b>{done}</b>closed with receipt</div>
  <div><b>{stats.get('cold', 0)}</b>cold artifacts</div>
</div>

<div class=grid>
  <div class=card><h3>It remembers</h3><p>Facts live in SQLite with full-text search.
  The prompt gets a relevance-ranked sample, not the whole history — long memory without
  context rot.</p></div>
  <div class=card><h3>It obliges</h3><p>An envelope names goal, task, recipient, token
  budget, mandate and price <em>before</em> work starts. Nothing surprising in the bill.</p></div>
  <div class=card><h3>It proves</h3><p>Every result is signed with Ed25519. The public key
  travels with the signature, so verification works offline — no call back to us.</p></div>
</div>

<h2>Connect</h2>
<pre>{{"mcpServers": {{"soma": {{"url": "https://rta.lat/hub/mcp",
  "headers": {{"Authorization": "Bearer $SOMA_TOKEN"}}}}}}}}</pre>
<p style="color:var(--dim);font-size:14px">Read-only demo, no token:
<code>GET /hub/status</code> · install notes:
<code>GET /hub/llms-install.md</code> · privacy: <code>GET /hub/privacy</code></p>

<h2>Try memory — no token</h2>
<form class=ask onsubmit="return false">
  <input id=q placeholder="how much does soma cost" autocomplete=off>
  <button type=button onclick=ask()>Ask</button>
</form>
<pre id=out data-facts="{stats.get('facts',0)}">Type a question. The node searches its own memory and shows what it finds.</pre>
<script>{_DEMO_JS}</script>

<h2>Tools</h2>
<table>{rows}</table>

<h2>What it costs</h2>
<table>
  <tr><td>free</td><td>read-only status, 20 verification calls a day, no card</td></tr>
  <tr><td>$49 / month</td><td>one environment: memory, obligations, receipts, dashboard</td></tr>
  <tr><td>$0.005</td><td>per envelope over x402 (USDC) — pay for what you use, no subscription</td></tr>
  <tr><td>1–2 %</td><td>node fee on paid work, publicly accounted</td></tr>
</table>

<footer>
  <span class=pill>● live</span> ·
  <a href="https://github.com/mrpkk/soma-protocol">source (MIT)</a> ·
  <a href="/hub/llms-install.md">install for agents</a> ·
  <a href="/hub/privacy">privacy</a> ·
  <a href="https://registry.modelcontextprotocol.io/v0/servers?search=io.github.mrpkk">MCP registry</a> ·
  <a href="mailto:maxim.pkk@gmail.com?subject=Soma%20token">get a token</a>
</footer>
</main>
"""

def _build_app(http_path: str, rate_limit: int = 0, force_visibility: str = ""):
    """ASGI-приложение MCP: transport-security и управляемый профиль."""
    import hmac
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    from .limits import IpRateLimit, PathAlias

    app = mcp.streamable_http_app(
        streamable_http_path=http_path,
        transport_security=TransportSecuritySettings(
            allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*",
                           "rta.lat", "*.rta.lat"]))

    # /privacy и /llms-install.md: чек-лист каталогов MCP отвергает сервер,
    # у которого политика отдаёт 404 (см. attest/docs/MCP-CATALOG-CHECKLIST.md).
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    prefix = http_path.rsplit("/", 1)[0]

    async def _privacy(request):
        return PlainTextResponse(_PRIVACY, media_type="text/plain; charset=utf-8")

    async def _llms(request):
        return PlainTextResponse(_LLMS_INSTALL, media_type="text/plain; charset=utf-8")

    async def _health(request):
        return JSONResponse({"ok": True, "service": "soma", "mcp_path": http_path})

    # ── публичный вход: лендинг и статус без токена ────────────────────────
    # Каталоги MCP и живые люди сначала смотрят, что здесь есть. 401 на
    # главной не пускал никого — это и было главным трением на входе.
    def _public_state() -> dict:
        st = hub().memory.stats()
        st.update({"service": "soma", "version": "1.2.0", "mcp_path": http_path,
                   "tools": [n for n, _ in _TOOLS], "managed": os.environ.get("SOMA_MANAGED") == "1"})
        return st

    async def _landing_route(request):
        from starlette.responses import HTMLResponse
        return HTMLResponse(_landing(_public_state()))

    async def _status_route(request):
        return JSONResponse(_public_state())

    for _p in ("/hub", "/hub/"):
        app.router.routes.insert(0, Route(_p, _landing_route))
    for _p in ("/hub/status", "/status"):
        app.router.routes.insert(0, Route(_p, _status_route))

    async def _export_route(request):
        h = hub()
        with h.memory.lock:
            facts = [dict(r) for r in h.memory.conn.execute(
                "SELECT id,text,tags,importance,kind,source,session,created_at,updated_at,status"
                " FROM facts ORDER BY id").fetchall()]
            envs = [dict(r) for r in h.memory.conn.execute(
                "SELECT * FROM envelopes ORDER BY created_at").fetchall()]
        return JSONResponse({"exported_at": time.time(),
                             "counts": {"facts": len(facts), "envelopes": len(envs)},
                             "facts": facts, "envelopes": envs})

    app.router.routes.insert(0, Route("/hub/export", _export_route))

    # ── демо памяти: попробовать до всякой регистрации ──────────────────────
    # Чтение только по фактам узла. Записи нет: демо не должно быть способом
    # залить чужую базу. Лимит в памяти процесса — 10 запросов в минуту на IP.
    import threading
    _demo_hits: dict[str, list[float]] = {}
    _demo_lock = threading.Lock()

    def _demo_allow(ip: str, per_min: int = 10) -> bool:
        now = time.time()
        with _demo_lock:
            hits = [t for t in _demo_hits.get(ip, []) if now - t < 60]
            if len(hits) >= per_min:
                _demo_hits[ip] = hits
                return False
            hits.append(now)
            _demo_hits[ip] = hits
            return True

    async def _demo_recall(request):
        ip = request.client.host if request.client else "?"
        if not _demo_allow(ip):
            return JSONResponse({"error": "rate limit: 10 requests per minute per address"},
                                status_code=429)
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return JSONResponse({"error": "q must be at least 2 characters"}, status_code=400)
        rows = hub().memory.recall(q, k=5)
        facts = [{"text": r.get("text", ""), "tags": r.get("tags", ""),
                  "importance": r.get("importance", 3)} for r in rows]
        return JSONResponse({"query": q, "count": len(facts), "facts": facts})

    app.router.routes.insert(0, Route("/hub/demo/recall", _demo_recall))

    for _p in ("/health", prefix + "/health"):
        app.router.routes.insert(0, Route(_p, _health))
    for _p in ("/privacy", prefix + "/privacy"):
        app.router.routes.insert(0, Route(_p, _privacy))
    for _p in ("/llms-install.md", prefix + "/llms-install.md"):
        app.router.routes.insert(0, Route(_p, _llms))

    # Управляемый профиль: обязательный bearer-токен.
    # Токен не настроен — значит вход закрыт (ошибка конфигурации не открывает дверь).
    token = os.environ.get("SOMA_TOKEN", "")
    if os.environ.get("SOMA_MANAGED") == "1":
        # Публично — только витрина и статус. Всё, что пишет или читает
        # чужие факты, по-прежнему требует токен.
        public_paths = {
            "/hub", "/hub/status", "/status", "/health", "/hub/health",
            "/privacy", "/hub/privacy", "/llms-install.md", "/hub/llms-install.md",
            "/hub/demo/recall",
        }

        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                path = request.url.path.rstrip("/") or "/"
                if path in public_paths:
                    return await call_next(request)
                got = request.headers.get("authorization") or ""
                if not token or not hmac.compare_digest(got.strip(), f"Bearer {token}"):
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerAuth)

    aliases = os.environ.get("SOMA_PATH_ALIASES", "")
    if aliases:
        # Путь публикуется в реестре, а живой эндпоинт остаётся прежним.
        app.add_middleware(PathAlias(http_path, aliases).as_middleware())
    if rate_limit > 0:
        app.add_middleware(IpRateLimit(rate_limit).as_middleware())
    if force_visibility:
        # Видимость читается инструментом в момент вызова — выставляем здесь,
        # чтобы параметр сборки приложения и поведение инструмента совпадали.
        os.environ["SOMA_FORCE_VISIBILITY"] = force_visibility
    return app


def _run_http(args) -> None:
    import uvicorn
    uvicorn.run(_build_app(args.http_path, args.rate_limit_per_hour, args.force_visibility),
                host=args.host, port=args.http, log_level="warning")


def _run_stdio() -> None:
    asyncio.run(mcp.run_stdio_async())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", type=int, default=0, help="порт streamable-http (0 = stdio)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8193") or 8193))
    ap.add_argument("--http-path", default="/hub/mcp",
                    help="путь MCP под префиксом туннеля (путь не срезается)")
    ap.add_argument("--rate-limit-per-hour", type=int,
                    default=int(os.environ.get("SOMA_RATE_LIMIT_PER_HOUR", "0") or 0),
                    help="лимит запросов в час на один IP (0 — без ограничения)")
    ap.add_argument("--force-visibility", default=os.environ.get("SOMA_FORCE_VISIBILITY", ""),
                    help="принудительная видимость для публичного узла (public)")
    args = ap.parse_args()
    if args.http:
        _run_http(args)
    else:
        _run_stdio()


if __name__ == "__main__":
    main()
