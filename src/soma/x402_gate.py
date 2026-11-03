"""x402-гейт: бесплатный лимит в день, дальше HTTP 402 по протоколу x402.

Зачем. До 04.11.2026 платный режим был только у attest и drift: остальные
серверы были бесплатны навсегда, и в каталогах им нечего было показать в
поле «цена». Гейт делает цену настоящей: после лимита сервер отвечает 402
с блоком `accepts[]`, который понимает любой x402-клиент.

Цена — канон `work/sales/ИТОГИ(ЭТАЛОНЫ)/tablets.md` §6: вызов $0.02,
подписка $19/мес. Два SKU на сервер, не больше.

Формат ответа и порядок расчёта повторяют attest/drift, проверенные живыми
платежами 17.10.2026: `/verify` у фасилитатора отвечает «подпись верна» и
денег не двигает, `/settle` переводит их. Без второго шага гейт отдаёт
работу, а баланс не меняется.

Конфигурация — переменные с префиксом `X402_GATE_`, а не `X402_`:
в `~/.env` уже лежат `X402_NETWORK=eip155:84532` (Sepolia) для тестов
платежей, и сервис, читающий этот файл, молча ушёл бы на тестовую сеть.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
USDC_DECIMALS = 6
MAINNET = "eip155:8453"
PAYAI = "https://facilitator.payai.network"

#: $0.02 и $19 — канон §6, в атомах USDC (6 знаков).
PRICE_ATOMS = 20_000
SUB_PRICE_ATOMS = 19_000_000

PAYMENT_HEADERS = ("x-payment", "payment-signature")
SUBS_HEADER = "x-subscription"
CHALLENGE_HEADER = "PAYMENT-REQUIRED"
SETTLEMENT_HEADER = "X-PAYMENT-RESPONSE"
MAX_BODY = 2 * 1024 * 1024

#: Методы MCP, не тратящие квоту: обнаружение, а не работа. Каталог,
#: проверяющий нас ежечасно, не должен оплачивать право узнать, что мы живы.
FREE_METHODS = frozenset({
    "initialize", "tools/list", "resources/list", "prompts/list",
    "ping", "notifications/initialized", "notifications/cancelled",
})


def _atoms_to_usd(atoms: int) -> str:
    return f"{atoms / 10 ** USDC_DECIMALS:.6f}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class Config:
    """Настройки гейта одного сервиса. Всё, что может отличаться, — здесь."""

    service: str
    resource: str
    pay_to: str
    network: str = MAINNET
    facilitator: str = PAYAI
    asset: str = USDC_BASE
    price_atoms: int = PRICE_ATOMS
    free_per_day: int = 100
    sub_price_atoms: int = SUB_PRICE_ATOMS
    sub_secret: str = ""

    @property
    def price_usd(self) -> str:
        return _atoms_to_usd(self.price_atoms)

    @property
    def sub_price_usd(self) -> str:
        return _atoms_to_usd(self.sub_price_atoms)

    @property
    def subscription_enabled(self) -> bool:
        return bool(self.sub_secret)


def config(service: str, resource: str) -> Config | None:
    """Собрать конфиг из окружения. ``None`` — платного режима нет.

    Нет адреса получателя — нет и гейта: сервер честно работает бесплатно,
    а не показывает цену, по которой некому платить.
    """
    pay_to = (os.environ.get("X402_GATE_PAY_TO") or "").strip()
    if not pay_to:
        return None
    return Config(
        service=service,
        resource=resource,
        pay_to=pay_to,
        network=(os.environ.get("X402_GATE_NETWORK") or MAINNET).strip(),
        facilitator=(os.environ.get("X402_GATE_FACILITATOR") or PAYAI).strip(),
        price_atoms=int(os.environ.get("X402_GATE_PRICE_ATOMS") or PRICE_ATOMS),
        free_per_day=int(os.environ.get("X402_GATE_FREE_PER_DAY") or 100),
        sub_price_atoms=int(os.environ.get("X402_GATE_SUB_PRICE_ATOMS") or SUB_PRICE_ATOMS),
        sub_secret=(os.environ.get("X402_GATE_SUB_SECRET") or "").strip(),
    )


def requirement(cfg: Config, description: str = "") -> dict[str, Any]:
    """Один элемент ``accepts[]`` — то, что клиент подписывает (форма v2).

    Внутри — только поля v2. `resource`, `description` и `mimeType` живут в
    верхнем объекте `resource`: в живом ответе фасилитатора 04.11.2026 те же
    поля внутри `accepts[]` заставляют его классифицировать требование как v1
    и ответить `invalid_payment_requirements`.
    """
    return {
        "scheme": "exact",
        "network": cfg.network,
        "amount": str(cfg.price_atoms),
        "asset": cfg.asset,
        "payTo": cfg.pay_to,
        "maxTimeoutSeconds": 300,
        "extra": {"name": "USDC", "version": "2", "decimals": USDC_DECIMALS},
    }


def challenge(cfg: Config, *, used: int = 0, description: str = "") -> dict[str, Any]:
    """Тело 402: ``x402Version``, ``resource``, ``accepts[]`` + наши поля.

    Наши поля (`price_usd`, `free_tier`) не обязательны протоколу, но делают
    ответ самодостаточным: агент видит цену и лимит, не запрашивая доки.
    """
    body: dict[str, Any] = {
        "x402Version": 2,
        "error": "payment_required",
        "resource": {
            "url": cfg.resource,
            "description": description or f"{cfg.service}: one paid call",
            "mimeType": "application/json",
        },
        "accepts": [requirement(cfg, description)],
        "price_usd": cfg.price_usd,
        "free_tier": {
            "limit_per_day": cfg.free_per_day,
            "used": used,
            "message": (f"{cfg.free_per_day} calls per day are free. This call costs "
                        f"${cfg.price_usd} USDC on Base via x402. Pay with any x402 "
                        f"client and retry the same request with an X-PAYMENT header."),
        },
    }
    if cfg.subscription_enabled:
        base = cfg.resource.rsplit("/", 1)[0]
        body["subscription"] = {
            "price_usd": cfg.sub_price_usd,
            "period_days": 30,
            "resource": f"{base}/subscribe",
            "message": (f"Or ${cfg.sub_price_usd} for 30 days without the daily cap: "
                        f"POST {base}/subscribe with the same x402 flow, then send the "
                        f"returned token in the {SUBS_HEADER} header."),
        }
    return body


def challenge_headers(cfg: Config, body: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Заголовок PAYMENT-REQUIRED — base64url от JSON, как требует v2."""
    raw = json.dumps(dict(body or challenge(cfg)), separators=(",", ":")).encode()
    return {
        CHALLENGE_HEADER: base64.urlsafe_b64encode(raw).decode().rstrip("="),
        "Cache-Control": "no-store",
    }


class PaymentRejected(Exception):
    """Платёж пришёл, но неверный. Клиенту — 402 с причиной."""


class FacilitatorDown(Exception):
    """Проверить платёж не удалось. Это не отказ: клиент должен повторить."""


def _post(url: str, path: str, payload: Mapping[str, Any], timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _decode(header: str) -> dict[str, Any]:
    """Разобрать заголовок оплаты. Клиенты шлют base64 (иногда base64url)."""
    text = (header or "").strip()
    if text.startswith("{"):
        return json.loads(text)
    text += "=" * (-len(text) % 4)
    try:
        return json.loads(base64.b64decode(text.translate(str.maketrans("-_", "+/"))))
    except (ValueError, TypeError) as exc:
        raise PaymentRejected(f"payment header is not base64 JSON: {exc}") from exc


def verify(cfg: Config, payment_header: str, *, description: str = "") -> dict[str, Any]:
    """Проверить и рассчитать платёж. Возвращает квитанцию для заголовка ответа.

    Nonce локально не ведём: повтор nonce отвергает сам контракт USDC при
    расчёте, а `/settle` — единственное место, где деньги двигаются.
    shortcut: локальный реестр nonce не нужен, пока расчёт идёт через /settle;
    если появится путь «выдать работу до расчёта» — реестр обязателен.
    """
    presented = _decode(payment_header)
    wire = requirement(cfg, description)
    try:
        result = _post(cfg.facilitator, "/verify", {
            "x402Version": 2,
            "paymentPayload": presented,
            "paymentRequirements": wire,
        })
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FacilitatorDown(str(exc)) from exc
    if not result.get("isValid"):
        raise PaymentRejected(str(result.get("invalidReason") or "invalid payment"))
    details = result.get("details") or {}
    paid = details.get("amount", result.get("amount"))
    if paid is not None and int(paid) != cfg.price_atoms:
        raise PaymentRejected(
            f"paid {_atoms_to_usd(int(paid))} USDC, required {cfg.price_usd}")
    payer = str(result.get("payer") or "")

    try:
        settled = _post(cfg.facilitator, "/settle", {
            "x402Version": 2,
            "paymentPayload": presented,
            "paymentRequirements": wire,
        })
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise FacilitatorDown(str(exc)) from exc
    if not settled.get("success"):
        raise PaymentRejected(
            str(settled.get("errorReason") or settled.get("errorMessage") or "not settled"))
    tx = str(settled.get("transaction") or "")
    if not tx:
        # «Оплачено» без транзакции — это бесплатная услуга.
        raise PaymentRejected("facilitator reported success without a transaction")
    receipt = {
        "transaction": tx,
        "payer": str(settled.get("payer") or payer),
        "amount": cfg.price_usd,
        "network": cfg.network,
    }
    return receipt


def receipt_header(receipt: Mapping[str, Any]) -> dict[str, str]:
    raw = json.dumps(dict(receipt), separators=(",", ":")).encode()
    return {SETTLEMENT_HEADER: base64.urlsafe_b64encode(raw).decode().rstrip("=")}


# ── подписка: 30 дней без дневного потолка, один платёж ─────────────────────
# Токен — HMAC от «сервис + срок». Он не даёт доступа к данным другого
# сервиса: имя сервиса входит в подпись, поэтому подписка на skilllint
# не открывает soma, даже если секрет общий.


def issue_token(cfg: Config, days: int = 30) -> str:
    exp = int(time.time()) + days * 86_400
    payload = f"{cfg.service}:{exp}"
    sig = hmac.new(cfg.sub_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")


def token_ok(cfg: Config, token: str) -> bool:
    if not cfg.subscription_enabled or not token:
        return False
    try:
        text = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        service, exp, sig = text.rsplit(":", 2)
    except (ValueError, TypeError):
        return False
    expected = hmac.new(cfg.sub_secret.encode(), f"{service}:{exp}".encode(),
                        hashlib.sha256).hexdigest()
    return (service == cfg.service and int(exp) > time.time()
            and hmac.compare_digest(sig, expected))


def subscribe(cfg: Config, payment_header: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Купить 30 дней. Без заголовка оплаты — 402 с чеком, с ним — токен."""
    sub = Config(**{**cfg.__dict__, "price_atoms": cfg.sub_price_atoms,
                    "resource": cfg.resource.rsplit("/", 1)[0] + "/subscribe"})
    if not cfg.subscription_enabled:
        return 503, {"error": "subscription_unavailable",
                     "message": "This deployment sells per-call access only."}, {}
    if not payment_header:
        body = challenge(sub, description=f"{cfg.service}: 30 days without the daily cap")
        return 402, body, challenge_headers(sub, body)
    try:
        receipt = verify(sub, payment_header,
                         description=f"{cfg.service}: 30 days without the daily cap")
    except FacilitatorDown as exc:
        return 503, {"error": "verification_unavailable", "reason": str(exc),
                     "message": "Retry with the same payment; it was not rejected."}, \
               {"Retry-After": "30"}
    except PaymentRejected as exc:
        body = challenge(sub, description=f"{cfg.service}: 30 days")
        return 402, {"error": "payment_rejected", "reason": str(exc)}, challenge_headers(sub, body)
    token = issue_token(cfg)
    return 200, {**receipt, "paid": True, "subscription_token": token,
                 "expires_in_days": 30,
                 "message": f"Send {SUBS_HEADER}: {token} with every request."}, \
           {SUBS_HEADER: token}


# ── бесплатный лимит ────────────────────────────────────────────────────────

def client_key(headers: Mapping[str, str], peer: str | None) -> str:
    """Кто звонит. За туннелем сокет всегда 127.0.0.1 — без CF-заголовка
    весь мир делит один счётчик, и первый сканер выбивает лимит у всех."""
    for name in ("cf-connecting-ip", "x-forwarded-for"):
        value = (headers.get(name) or "").strip()
        if value:
            return value.split(",")[0].strip()
    return peer or "unknown"


class DailyQuota:
    """Фиксированное окно в сутки, счётчик в памяти процесса.

    shortcut: окно сбрасывается при перезапуске и на границе суток клиент
    может получить 2×лимит. Для бесплатного тарифа это потолок CPU, не денег;
    когда лимит станет биллинговым — вынести в общее хранилище.
    """

    def __init__(self, limit: int, clock: Callable[[], float] = time.time) -> None:
        self.limit = max(0, int(limit))
        self._clock = clock
        self._counts: dict[tuple[str, int], int] = {}

    def hit(self, key: str) -> tuple[bool, int, int]:
        """(пускать ли, сколько уже потрачено, через сколько секунд сброс)."""
        if self.limit <= 0:
            return True, 0, 0
        now = self._clock()
        day = int(now // 86_400)
        slot = (key, day)
        used = self._counts.get(slot, 0)
        if used >= self.limit:
            return False, used, int(86_400 - now % 86_400)
        self._counts[slot] = used + 1
        if len(self._counts) > 8192:  # старые сутки не должны копиться
            self._counts = {k: v for k, v in self._counts.items() if k[1] >= day - 1}
        return True, used + 1, 0

    def used(self, key: str) -> int:
        return self._counts.get((key, int(self._clock() // 86_400)), 0)


def is_free_method(raw: bytes) -> bool:
    """Не разобрали тело — считаем платным: ошибка в сторону платности
    безопаснее, иначе квота перестаёт что-либо ограничивать."""
    try:
        payload = json.loads(raw.decode("utf-8", "ignore") or "{}")
    except (ValueError, TypeError):
        return False
    if isinstance(payload, list):
        return bool(payload) and all(
            isinstance(p, dict) and p.get("method") in FREE_METHODS for p in payload)
    return isinstance(payload, dict) and payload.get("method") in FREE_METHODS


# ── страница цены: то, на что ведёт websiteUrl в server.json ────────────────

def pricing_page(cfg: Config, *, title: str, what: str, docs: str) -> str:
    """Цена одной страницей. Каталоги читают её, человек — тоже.

    Правило §6: никаких «от» и «свяжитесь с нами» — только цифра.
    """
    subs = ""
    if cfg.subscription_enabled:
        subs = (f"<p><b>${cfg.sub_price_usd} / 30 days</b> — no daily cap. "
                f"<code>POST {cfg.resource.rsplit('/', 1)[0]}/subscribe</code>, "
                f"same wallet, then send <code>{SUBS_HEADER}</code>.</p>")
    else:
        subs = "<p>Pay per call. No subscription on this deployment.</p>"
    return f"""<!doctype html><html lang="en"><head><meta charset=utf-8>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — price</title>
<meta name="description" content="{what}">
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"SoftwareApplication","name":"{title}",
"applicationCategory":"DeveloperApplication","operatingSystem":"Any",
"description":"{what}","offers":{{"@type":"Offer","price":"{cfg.price_usd}",
"priceCurrency":"USD"}}}}
</script>
<style>
body{{background:#0b0d12;color:#e8ecf4;font:16px/1.6 system-ui,sans-serif;
max-width:720px;margin:52px auto;padding:0 22px}}
h1{{font-size:26px;margin:0 0 10px}}p{{color:#b9c2cf}}code{{background:#151a22;
padding:2px 6px;border-radius:4px}}a{{color:#58a6ff}}
.card{{border:1px solid #1e2430;border-radius:10px;padding:14px 16px;margin:14px 0}}
</style></head><body>
<h1>{title}</h1>
<p>{what}</p>
<div class="card">
<p><b>Free</b> — {cfg.free_per_day} calls per day, forever. Discovery
(<code>initialize</code>, <code>tools/list</code>) is always free.</p>
<p><b>${cfg.price_usd} per call</b> — USDC on Base mainnet over
<a href="https://x402.org">x402</a>. No account, no API key: ask, sign, retry.</p>
{subs}
</div>
<h2>Endpoint</h2>
<p><code>{cfg.resource}</code></p>
<h2>Receiving address</h2>
<p><code>{cfg.pay_to}</code> — the same wallet for every product, so revenue
does not scatter.</p>
<p><a href="{docs}">Install and documentation</a></p>
</body></html>"""


# ── ASGI-обёртка ───────────────────────────────────────────────────────────

class _Replay:
    """Отдать уже прочитанное тело и дальше пропускать настоящий receive."""

    def __init__(self, messages: list[dict], receive: Callable):
        self._messages = messages
        self._receive = receive
        self._i = 0

    async def __call__(self) -> dict:
        if self._i < len(self._messages):
            self._i += 1
            return self._messages[self._i - 1]
        return await self._receive()


def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])}


def middleware(cfg: Config, quota: DailyQuota, *, mcp_suffix: str = "/mcp"):
    """ASGI-обёртка: платная работа после лимита, обнаружение — бесплатно.

    Ставится снаружи приложения (`app = middleware(...)(app)`), потому что
    Starlette добавляет middleware внутрь, и 402 должен решаться до парсера.
    """
    from starlette.responses import JSONResponse

    class PaymentGate:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http" or scope.get("method") != "POST" \
                    or not scope.get("path", "").rstrip("/").endswith(mcp_suffix):
                await self.app(scope, receive, send)
                return
            hdrs = _headers(scope)
            messages: list[dict] = []
            body = b""
            while True:
                msg = await receive()
                messages.append(msg)
                if msg["type"] != "http.request":
                    break
                body += msg.get("body", b"")
                if len(body) > MAX_BODY:
                    await JSONResponse({"error": "payload too large"},
                                       status_code=413)(scope, receive, send)
                    return
                if not msg.get("more_body"):
                    break
            replay = _Replay(messages, receive)

            if is_free_method(body) or token_ok(cfg, hdrs.get(SUBS_HEADER, "")):
                await self.app(scope, replay, send)
                return

            payment = next((hdrs[h] for h in PAYMENT_HEADERS if hdrs.get(h)), "")
            extra: dict[str, str] = {}
            if payment:
                try:
                    extra = receipt_header(verify(cfg, payment))
                except FacilitatorDown as exc:
                    # Не «отказ»: мы не смогли проверить. Клиент повторяет.
                    await JSONResponse(
                        {"error": "verification_unavailable", "reason": str(exc),
                         "message": "The payment was not rejected. Retry with the same payment."},
                        status_code=503, headers={"Retry-After": "30"})(scope, receive, send)
                    return
                except PaymentRejected as exc:
                    await JSONResponse({"error": "payment_rejected", "reason": str(exc)},
                                       status_code=402,
                                       headers=challenge_headers(cfg))(scope, receive, send)
                    return
                await self.app(scope, replay, _with_headers(send, extra))
                return

            key = client_key(hdrs, (scope.get("client") or [None])[0])
            allowed, used, retry_after = quota.hit(key)
            if allowed:
                await self.app(scope, replay, send)
                return
            body402 = challenge(cfg, used=used)
            headers = {**challenge_headers(cfg, body402), "Retry-After": str(retry_after)}
            await JSONResponse(body402, status_code=402,
                               headers=headers)(scope, receive, send)

    return PaymentGate


def _with_headers(send: Callable, extra: Mapping[str, str]) -> Callable:
    async def _send(message: dict) -> None:
        if message["type"] == "http.response.start" and extra:
            message = dict(message)
            message["headers"] = list(message.get("headers") or []) + [
                (k.lower().encode(), v.encode()) for k, v in extra.items()]
        await send(message)
    return _send
