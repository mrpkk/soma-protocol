"""Тесты x402-гейта. Запуск: python3 -m pytest tests/ -q

Проверяется то, что нельзя проверить глазами в ответе сервера: что лимит
считается, что 402 несёт цену, и что подписка одного сервиса не открывает
другой.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from soma import x402_gate as gate  # noqa: E402


def _cfg(**kw) -> gate.Config:
    base = dict(service="soma", resource="https://rta.lat/soma/mcp",
                pay_to="0x0000000000000000000000000000000000000001")
    base.update(kw)
    return gate.Config(**base)


def test_price_matches_canon():
    """§6 tablets.md: вызов $0.02, подписка $19. Не «примерно» — цифрой."""
    cfg = _cfg()
    assert cfg.price_usd == "0.02"
    assert cfg.sub_price_usd == "19"


def test_challenge_carries_price_and_payee():
    body = gate.challenge(_cfg(), used=42)
    assert body["x402Version"] == 2
    assert body["price_usd"] == "0.02"
    accept = body["accepts"][0]
    assert accept["amount"] == "20000" and accept["asset"] == gate.USDC_BASE
    assert accept["payTo"] == "0x0000000000000000000000000000000000000001"
    assert accept["network"] == "eip155:8453"
    assert body["free_tier"]["used"] == 42 and body["free_tier"]["limit_per_day"] == 100


def test_challenge_header_is_parseable_json():
    import base64
    import json
    raw = gate.challenge_headers(_cfg())["PAYMENT-REQUIRED"]
    decoded = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert decoded["accepts"][0]["amount"] == "20000"


def test_quota_counts_and_stops():
    q = gate.DailyQuota(limit=2, clock=lambda: 1_700_000_000.0)
    assert q.hit("a")[:2] == (True, 1)
    assert q.hit("a")[0] is True
    allowed, used, retry_after = q.hit("a")
    assert allowed is False and used == 2 and retry_after > 0
    assert q.hit("b")[0] is True, "чужой ключ не должен тратить лимит"


def test_quota_resets_next_day():
    now = [1_700_000_000.0]
    q = gate.DailyQuota(limit=1, clock=lambda: now[0])
    q.hit("a")
    assert q.hit("a")[0] is False
    now[0] += 86_400
    assert q.hit("a")[0] is True


def test_free_methods_are_free():
    assert gate.is_free_method(b'{"method":"initialize"}')
    assert gate.is_free_method(b'{"method":"tools/list"}')
    assert not gate.is_free_method(b'{"method":"tools/call"}')
    assert not gate.is_free_method(b"")          # не разобрали — платно
    assert not gate.is_free_method(b"[]")        # пустой батч — не «всё бесплатно»


def test_subscription_token_is_bound_to_one_service():
    cfg = _cfg(sub_secret="s3cret")
    token = gate.issue_token(cfg)
    assert gate.token_ok(cfg, token)
    other = _cfg(service="mitracore", sub_secret="s3cret")
    assert not gate.token_ok(other, token), "подписка не должна переноситься"
    assert not gate.token_ok(_cfg(), token), "без секрета подписки нет"


def test_expired_subscription_is_rejected():
    cfg = _cfg(sub_secret="s3cret")
    token = gate.issue_token(cfg, days=0)
    assert not gate.token_ok(cfg, token)


def test_client_key_prefers_cloudflare_header():
    """За туннелем peer = 127.0.0.1; ключ обязан брать реальный адрес."""
    assert gate.client_key({"cf-connecting-ip": "1.2.3.4"}, "127.0.0.1") == "1.2.3.4"
    assert gate.client_key({"x-forwarded-for": "5.6.7.8, 10.0.0.1"}, None) == "5.6.7.8"
    assert gate.client_key({}, "127.0.0.1") == "127.0.0.1"


def test_env_config_absent_means_free_mode():
    for name in ("X402_GATE_PAY_TO",):
        os.environ.pop(name, None)
    assert gate.config("skilllint", "https://rta.lat/skilllint/mcp") is None
