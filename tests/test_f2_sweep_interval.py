"""F2 — background sweep_interval_minutes (default 2) + loop invokes sweep."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.config import CONFIG_KEYS, get_guild_config, resolve_config, set_config
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
