"""Подпись: исторический формат проверяется, подделка — нет.

Ранние конверты подписывались с пустым мандатом `{}` в теле. Сейчас пустой
мандат в тело не попадает, и строгая проверка объявляла такие конверты
подделкой. Это неверно в обе стороны: нельзя терять доверие к настоящим
подписям и нельзя прощать изменённое тело.
"""
from __future__ import annotations

import os

from soma.envelope import Ed25519Signer, Envelope


def _env() -> Envelope:
    return Envelope(goal="g", task="t", to="*", sender="a", token_budget=100)


def test_legacy_signature_with_empty_mandate_verifies():
    signer = Ed25519Signer(key=os.urandom(32))
    env = _env()
    env.mandate = {}                    # так подписывали раньше
    sig = signer.sign(env)
    env.mandate = None                  # так конверт читается сейчас
    ok, why = signer.verify(env)
    assert ok, why
    assert "исторический" in why


def test_current_format_verifies_as_ok():
    signer = Ed25519Signer(key=os.urandom(32))
    env = _env()
    signer.sign(env)
    ok, why = signer.verify(env)
    assert (ok, why) == (True, "ok")


def test_tampered_body_fails_even_with_legacy_path():
    signer = Ed25519Signer(key=os.urandom(32))
    env = _env()
    env.mandate = {}
    env.signature = signer.sign(env)
    env.mandate = None
    env.task = "другая задача"          # подмена после подписи
    ok, why = signer.verify(env)
    assert not ok and "не сошлась" in why


def test_foreign_client_key_is_refused():
    """Ключ едет в подписи, поэтому происхождение сверяется с реестром отдельно."""
    signer = Ed25519Signer(key=os.urandom(32))
    env = _env()
    env.mandate = {}
    sig = signer.sign(env)
    sig["signer"] = "client"
    env.signature = sig
    env.mandate = None
    ok, why = signer.verify(env, expected_public_key="AAAA")
    assert not ok and "ключ" in why
