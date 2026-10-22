from soma.memory import Memory  # noqa: E402

def test_token_estimate_is_honest_for_russian_and_english(tmp_path):
    """Шкала токенов: русский ≈2.2 символа на токен, английский ≈4.

    23.10.2026: прежняя конвенция «220 символов на токен» занижала расход в 50–100 раз,
    и бюджет конверта выглядел соблюдённым при переполненном контексте.
    """
    from soma.memory import estimate_tokens

    ru = "Память агента должна переживать сессии и смену модели" * 5
    en = "Agent memory must survive sessions and model changes" * 5
    assert estimate_tokens(ru) > len(ru) / 3.0
    assert estimate_tokens(en) < estimate_tokens(ru)
    assert estimate_tokens("") == 0.0

    m = Memory(str(tmp_path / "m.db"), cold_dir=str(tmp_path / "cold"))
    fid = m.remember(ru, tags="ru")
    env = {"goal": "цель", "task": "задача", "token_budget": 10, "context_query": "память"}
    packed = m.pack(env)
    assert packed["tokens"] > 0
    assert isinstance(packed.get("dropped"), list)
    assert fid > 0
