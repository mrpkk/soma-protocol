"""Витрина офлайн-проверки: страница обязана показывать настоящую подпись.

Проверка в браузере — единственное место, где подпись Soma проверяется без
доверия к нашему серверу. Если на страницу попадёт подделанный или несогласованный
конверт, демонстрация станет ложью — поэтому конверт здесь проверяется ещё и
серверным кодом, до того как уйти в HTML.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from soma.envelope import Envelope, verify as verify_envelope  # noqa: E402
from soma.mcp_adapter import _sample_envelope, _verify_page  # noqa: E402


def test_sample_envelope_really_verifies():
    """Конверт со страницы проходит проверку тем же кодом, что и протокол."""
    data = _sample_envelope()
    env = Envelope.from_dict(data)
    ok, why = verify_envelope(env)
    assert ok, why
    assert env.signature["algorithm"] == "ed25519"


def test_page_embeds_the_signed_envelope():
    data = _sample_envelope()
    page = _verify_page(data)
    signature = data["signature"]["signature"]
    public_key = data["signature"]["public_key"]
    assert signature in page
    assert public_key in page
    assert "__ENVELOPE__" not in page, "плейсхолдер не подставлен"
    # Проверка идёт в браузере: никаких запросов со страницы к нашему API.
    assert "crypto.subtle.verify" in page
    assert "fetch(" not in page


def test_tampered_envelope_does_not_verify():
    """Ровно то, что делает кнопка «Испортить»: подпись обязана отвалиться."""
    data = _sample_envelope()
    data["goal"] = data["goal"] + " (изменено после подписи)"
    ok, _ = verify_envelope(Envelope.from_dict(data))
    assert not ok
