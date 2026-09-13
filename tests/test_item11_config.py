"""V2 item 11 — /config + guild overrides (§7)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.config import (
    CONFIG_KEYS,
    ConfigError,
    channel_allowed,
    format_config_value,
    get_guild_config,
    list_config_lines,
    resolve_config,
    set_config,
)
from bot.db import CourtDB
from bot.fights import (
    balance_free_counter_enabled,
    balance_warn_below,
    challenge_timeout_hours,
    counters_per_side,
    rest_timeout_hours,
    thread_archive_delay_hours,
)
from bot.limits import RateLimiter, cooldown_seconds, guild_daily_cap
from bot.budget import monthly_usd_cap
from bot.transcript import transcript_max_tokens


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def test_defaults_match_section_7(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear §7 env overrides so hardcoded defaults surface.
    for _k, (_d, env_name, _t) in CONFIG_KEYS.items():
        if env_name:
            monkeypatch.delenv(env_name, raising=False)
    db = _db(tmp_path)
    resolved = resolve_config(db, guild_id=1)
    assert resolved["counters_per_side"] == 2
    assert resolved["challenge_timeout_hours"] == 6.0
    assert resolved["rest_timeout_hours"] == 24.0
    assert resolved["balance_warn_below"] == 4.0
    assert resolved["balance_free_counter"] is True
    assert resolved["cooldown_seconds"] == 60
    assert resolved["daily_cap"] == 50
    assert resolved["monthly_usd_cap"] == 20.0
    assert resolved["allowed_channels"] == []
    assert resolved["thread_archive_delay_hours"] == 24.0
    assert resolved["transcript_max_tokens"] == 20_000
    db.close()


def test_precedence_env_then_guild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setenv("FIGHT_CHALLENGE_TIMEOUT_HOURS", "3")
    monkeypatch.setenv("FIGHT_COUNTERS_PER_SIDE", "5")
    monkeypatch.setenv("FIGHT_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("FIGHT_GUILD_DAILY_CAP", "10")
    monkeypatch.setenv("FIGHT_MONTHLY_USD_CAP", "5")
    monkeypatch.setenv("FIGHT_BALANCE_WARN_BELOW", "7")
    monkeypatch.setenv("FIGHT_BALANCE_FREE_COUNTER", "0")
    monkeypatch.setenv("FIGHT_REST_TIMEOUT_HOURS", "12")
    monkeypatch.setenv("FIGHT_THREAD_ARCHIVE_DELAY_HOURS", "8")
    monkeypatch.setenv("FIGHT_TRANSCRIPT_MAX_TOKENS", "10000")

    assert challenge_timeout_hours(db, None) == 3.0
    assert counters_per_side(db, None) == 5
    assert cooldown_seconds(None, None) == 30
    assert guild_daily_cap(None, None) == 10
    assert monthly_usd_cap(None, None) == 5.0
    assert balance_warn_below(db, None) == 7.0
    assert balance_free_counter_enabled(db, None) is False
    assert rest_timeout_hours(db, None) == 12.0
    assert thread_archive_delay_hours(db, None) == 8.0
    assert transcript_max_tokens(db, None) == 10_000

    # Guild overrides beat env.
    set_config(db, 7, "challenge_timeout_hours", "9")
    set_config(db, 7, "counters_per_side", "1")
    set_config(db, 7, "cooldown_seconds", "120")
    set_config(db, 7, "daily_cap", "99")
    set_config(db, 7, "monthly_usd_cap", "50")
    set_config(db, 7, "balance_warn_below", "2")
    set_config(db, 7, "balance_free_counter", "true")
    set_config(db, 7, "rest_timeout_hours", "48")
    set_config(db, 7, "thread_archive_delay_hours", "1")
    set_config(db, 7, "transcript_max_tokens", "5000")

    assert challenge_timeout_hours(db, 7) == 9.0
    assert counters_per_side(db, 7) == 1
    assert cooldown_seconds(db, 7) == 120
    assert guild_daily_cap(db, 7) == 99
    assert monthly_usd_cap(db, 7) == 50.0
    assert balance_warn_below(db, 7) == 2.0
    assert balance_free_counter_enabled(db, 7) is True
    assert rest_timeout_hours(db, 7) == 48.0
    assert thread_archive_delay_hours(db, 7) == 1.0
    assert transcript_max_tokens(db, 7) == 5000

    # Other guild still sees env.
    assert challenge_timeout_hours(db, 8) == 3.0
    db.close()


def test_set_list_readback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for _k, (_d, env_name, _t) in CONFIG_KEYS.items():
        if env_name:
            monkeypatch.delenv(env_name, raising=False)
    db = _db(tmp_path)
    set_config(db, 1, "daily_cap", "42")
    assert get_guild_config(db, 1, "daily_cap") == 42
    assert db.get_guild_config(1, "daily_cap") == "42"
    lines = list_config_lines(db, 1)
    assert any("`daily_cap` = 42" in line for line in lines)
    assert len(lines) == len(CONFIG_KEYS)
    db.close()


def test_invalid_key_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    with pytest.raises(ConfigError, match="unknown config key"):
        set_config(db, 1, "not_a_real_key", "1")
    with pytest.raises(ConfigError, match="unknown config key"):
        get_guild_config(db, 1, "nope")
    db.close()


def test_allowed_channels_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIGHT_ALLOWED_CHANNELS", raising=False)
    db = _db(tmp_path)
    assert channel_allowed(db, 1, 100) is True  # empty = all
    set_config(db, 1, "allowed_channels", "111,222")
    assert get_guild_config(db, 1, "allowed_channels") == [111, 222]
    assert channel_allowed(db, 1, 111) is True
    assert channel_allowed(db, 1, 999) is False
    set_config(db, 1, "allowed_channels", "[]")
    assert channel_allowed(db, 1, 999) is True
    db.close()


def test_limiter_uses_guild_cooldown_and_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setenv("FIGHT_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("FIGHT_GUILD_DAILY_CAP", "100")
    set_config(db, 1, "cooldown_seconds", "60")
    set_config(db, 1, "daily_cap", "1")
    lim = RateLimiter()
    assert lim.check(1, 1, db=db) is None
    lim.record(1, 1)
    msg = lim.check(1, 1, db=db)
    assert msg is not None
    assert "Cooldown" in msg
    lim.clear()
    lim.record(1, 1)
    # Different user still hits daily cap of 1
    msg2 = lim.check(2, 1, db=db)
    assert msg2 is not None
    assert "cap" in msg2.lower()
    db.close()


def _admin_interaction(*, guild_id: int = 1, manage: bool = True) -> MagicMock:
    user = MagicMock()
    user.guild_permissions = MagicMock()
    user.guild_permissions.manage_guild = manage
    interaction = MagicMock()
    interaction.guild_id = guild_id
    interaction.user = user
    interaction.channel_id = 555
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def test_config_cmd_list_and_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    for _k, (_d, env_name, _t) in CONFIG_KEYS.items():
        if env_name:
            monkeypatch.delenv(env_name, raising=False)

    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)

    # Non-admin rejected
    denied = _admin_interaction(manage=False)
    asyncio.run(cog.config_cmd.callback(cog, denied, key=None, value=None))
    denied.response.send_message.assert_awaited()
    assert "Manage Server" in denied.response.send_message.await_args.args[0]

    # List
    listing = _admin_interaction()
    asyncio.run(cog.config_cmd.callback(cog, listing, key=None, value=None))
    kw = listing.response.send_message.await_args.kwargs
    embed = kw["embed"]
    assert "Fight Club config" in embed.title
    assert "counters_per_side" in (embed.description or "")
    assert kw.get("ephemeral") is True

    # Set + readback
    setting = _admin_interaction()
    asyncio.run(
        cog.config_cmd.callback(cog, setting, key="daily_cap", value="77")
    )
    assert "daily_cap" in setting.response.send_message.await_args.args[0]
    assert get_guild_config(db, 1, "daily_cap") == 77

    # Invalid key
    bad = _admin_interaction()
    asyncio.run(cog.config_cmd.callback(cog, bad, key="bogus", value="1"))
    assert "unknown" in bad.response.send_message.await_args.args[0].lower()
    db.close()


def test_fight_rejects_outside_allowed_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    set_config(db, 1, "allowed_channels", "100,200")

    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)
    interaction = _admin_interaction()
    interaction.channel_id = 999  # not allowed
    interaction.guild_id = 1

    asyncio.run(
        cog.fight.callback(
            cog,
            interaction,
            opponent=None,
            matchup=None,
            context=None,
            side=None,
            instant=False,
            fighter_a=None,
            fighter_b=None,
            franchise=None,
            exhibits=None,
        )
    )
    interaction.response.send_message.assert_awaited()
    msg = interaction.response.send_message.await_args.args[0]
    assert "not allowed" in msg.lower()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    db.close()


def test_format_allowed_channels_empty() -> None:
    assert format_config_value([], "allowed_channels") == "[]"
    assert format_config_value([1, 2], "allowed_channels") == "[1, 2]"
