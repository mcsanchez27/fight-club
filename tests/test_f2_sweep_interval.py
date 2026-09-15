"""F2 — background sweep_interval_minutes (default 2) + loop invokes sweep."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.config import (
    CONFIG_KEYS,
    get_guild_config,
    loop_sweep_interval,
    resolve_config,
    set_config,
)
from bot.db import CourtDB


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def test_sweep_interval_default_is_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FIGHT_SWEEP_INTERVAL_MINUTES", raising=False)
    db = _db(tmp_path)
    assert get_guild_config(db, None, "sweep_interval_minutes") == 2
    assert resolve_config(db, None)["sweep_interval_minutes"] == 2
    assert CONFIG_KEYS["sweep_interval_minutes"][0] == 2
    db.close()


def test_sweep_interval_env_and_guild_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setenv("FIGHT_SWEEP_INTERVAL_MINUTES", "5")
    assert get_guild_config(db, None, "sweep_interval_minutes") == 5
    set_config(db, 42, "sweep_interval_minutes", "1")
    assert get_guild_config(db, 42, "sweep_interval_minutes") == 1
    # Other guild still sees env.
    assert get_guild_config(db, 99, "sweep_interval_minutes") == 5
    db.close()


def test_background_loop_invokes_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """FightCog helper calls sweep_deadlines with archive callback (mock; no bot)."""
    monkeypatch.delenv("FIGHT_SWEEP_INTERVAL_MINUTES", raising=False)
    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)
    cog.bot = MagicMock()
    loop = MagicMock()
    cog.rejudge_loop = loop

    called: dict = {}

    def fake_sweep(db, now, *, archive_thread=None):
        called["db"] = db
        called["now"] = now
        called["archive_thread"] = archive_thread
        return {"expired": [], "judge_ready": [], "archived": []}

    fake_db = MagicMock()
    with (
        patch("bot.commands.get_db", return_value=fake_db),
        patch("bot.commands.utc_now", return_value="2026-09-13T18:00:00+00:00"),
        patch("bot.commands.sweep_deadlines", side_effect=fake_sweep),
        patch("bot.commands.get_guild_config", return_value=2),
    ):
        asyncio.run(cog._run_background_sweep())

    assert called["db"] is fake_db
    assert called["now"] == "2026-09-13T18:00:00+00:00"
    assert callable(called["archive_thread"])
    loop.change_interval.assert_called_with(minutes=2.0)


def test_rejudge_loop_calls_sweep_then_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)
    cog.bot = MagicMock()
    order: list[str] = []

    async def fake_sweep():
        order.append("sweep")

    async def fake_queue():
        order.append("queue")

    cog._run_background_sweep = fake_sweep  # type: ignore[method-assign]
    cog._process_rejudge_queue = fake_queue  # type: ignore[method-assign]

    asyncio.run(FightCog.rejudge_loop.coro(cog))
    assert order == ["sweep", "queue"]


# --- F4: one loop, many guilds — tick at the tightest interval asked for ------


def test_loop_interval_uses_global_when_no_guild_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FIGHT_SWEEP_INTERVAL_MINUTES", raising=False)
    db = _db(tmp_path)
    assert loop_sweep_interval(db) == 2.0
    monkeypatch.setenv("FIGHT_SWEEP_INTERVAL_MINUTES", "5")
    assert loop_sweep_interval(db) == 5.0
    db.close()


def test_loop_interval_tightens_to_fastest_guild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guild asking for 1 gets 1 — the loop cannot tick per-guild (NOTES 70)."""
    monkeypatch.setenv("FIGHT_SWEEP_INTERVAL_MINUTES", "5")
    db = _db(tmp_path)
    set_config(db, 42, "sweep_interval_minutes", "1")
    set_config(db, 99, "sweep_interval_minutes", "10")
    assert loop_sweep_interval(db) == 1.0
    # Per-guild resolution is untouched — only the loop reconciles.
    assert get_guild_config(db, 42, "sweep_interval_minutes") == 1
    assert get_guild_config(db, 99, "sweep_interval_minutes") == 10
    assert get_guild_config(db, 7, "sweep_interval_minutes") == 5
    db.close()


def test_loop_interval_ignores_slower_guilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overrides slower than the global never slow the loop down."""
    monkeypatch.setenv("FIGHT_SWEEP_INTERVAL_MINUTES", "3")
    db = _db(tmp_path)
    set_config(db, 42, "sweep_interval_minutes", "30")
    assert loop_sweep_interval(db) == 3.0
    db.close()


def test_loop_interval_ignores_junk_and_nonpositive_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows written outside set_config must not stall or crash the loop."""
    monkeypatch.setenv("FIGHT_SWEEP_INTERVAL_MINUTES", "4")
    db = _db(tmp_path)
    db.set_guild_config(1, "sweep_interval_minutes", "0")
    db.set_guild_config(2, "sweep_interval_minutes", "-5")
    db.set_guild_config(3, "sweep_interval_minutes", "banana")
    assert loop_sweep_interval(db) == 4.0
    db.close()


def test_loop_interval_tolerates_no_db() -> None:
    assert loop_sweep_interval(None) > 0


def test_background_sweep_applies_tightest_guild_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: a guild override actually retunes the live loop."""
    monkeypatch.setenv("FIGHT_SWEEP_INTERVAL_MINUTES", "5")
    from bot.commands import FightCog

    db = _db(tmp_path)
    set_config(db, 42, "sweep_interval_minutes", "1")

    cog = FightCog.__new__(FightCog)
    cog.bot = MagicMock()
    loop = MagicMock()
    cog.rejudge_loop = loop

    with (
        patch("bot.commands.get_db", return_value=db),
        patch("bot.commands.utc_now", return_value="2026-09-14T18:00:00+00:00"),
        patch(
            "bot.commands.sweep_deadlines",
            return_value={"expired": [], "judge_ready": [], "archived": []},
        ),
    ):
        asyncio.run(cog._run_background_sweep())

    loop.change_interval.assert_called_with(minutes=1.0)
    db.close()
