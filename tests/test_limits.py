"""Per-user cooldown and per-guild daily cap."""

import time

from bot.limits import RateLimiter


def test_cooldown_rejects_second_call(monkeypatch) -> None:
    monkeypatch.setenv("FIGHT_COOLDOWN_SECONDS", "60")
    monkeypatch.setenv("FIGHT_GUILD_DAILY_CAP", "50")
    lim = RateLimiter()
    assert lim.check(user_id=1, guild_id=10) is None
    lim.record(1, 10)
    msg = lim.check(user_id=1, guild_id=10)
    assert msg is not None
    assert "Cooldown" in msg


def test_different_users_independent_cooldown(monkeypatch) -> None:
    monkeypatch.setenv("FIGHT_COOLDOWN_SECONDS", "60")
    lim = RateLimiter()
    lim.record(1, 10)
    assert lim.check(user_id=2, guild_id=10) is None


def test_guild_daily_cap(monkeypatch) -> None:
    monkeypatch.setenv("FIGHT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("FIGHT_GUILD_DAILY_CAP", "2")
    lim = RateLimiter()
    assert lim.check(1, 99) is None
    lim.record(1, 99)
    assert lim.check(2, 99) is None
    lim.record(2, 99)
    msg = lim.check(3, 99)
    assert msg is not None
    assert "cap" in msg.lower()


def test_cooldown_expires(monkeypatch) -> None:
    monkeypatch.setenv("FIGHT_COOLDOWN_SECONDS", "1")
    monkeypatch.setenv("FIGHT_GUILD_DAILY_CAP", "50")
    lim = RateLimiter()
    lim.record(1, 10)
    lim._user_last[1] = time.time() - 2
    assert lim.check(1, 10) is None
