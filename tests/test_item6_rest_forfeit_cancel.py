"""V2 item 6 — /rest, /forfeit, /cancel, judge_due_rests (no Sonnet)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.db import CourtDB
from bot.fights import (
    JUDGE_READY_STATUS,
    accept_fight,
    cancel_fight,
    create_proposed_fight,
    forfeit_fight,
    judge_due_rests,
    mark_judge_ready,
    rest_fight,
    rest_timeout_hours,
    to_iso,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _arguing(db: CourtDB, *, thread_id: int = 9001) -> dict:
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Goku",
        side_b="Vegeta",
        context="open field",
        now=FROZEN,
    )
    fid = int(fight["id"])
    out = accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=thread_id)
    assert out["status"] == "arguing"
    return out


def test_rest_timeout_hours_guild_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.delenv("FIGHT_REST_TIMEOUT_HOURS", raising=False)
    assert rest_timeout_hours(db, None) == 24.0
    monkeypatch.setenv("FIGHT_REST_TIMEOUT_HOURS", "12")
    assert rest_timeout_hours(db, None) == 12.0
    db.set_guild_config(1, "rest_timeout_hours", "6")
    assert rest_timeout_hours(db, 1) == 6.0
    db.close()


def test_first_rest_sets_resting_and_deadline(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    out = rest_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    assert out["status"] == "resting"
    assert out["rest_a_at"] == to_iso(FROZEN)
    assert out["rest_b_at"] is None
    assert out["rest_deadline_at"] == to_iso(FROZEN + timedelta(hours=24))
    db.close()


def test_empty_rest_allowed_amendment_7(tmp_path: Path) -> None:
    """Amendment 7: /rest with zero thread messages is fine (thin-record later)."""
    db = _db(tmp_path)
    fight = _arguing(db)
    # No message-count check — rest_fight only needs advocate + status.
    out = rest_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    assert out["status"] == "resting"
    db.close()


def test_second_rest_single_judge_ready_transition(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    rest_fight(db, fid, now=FROZEN, actor_id=10)
    transitions: list[bool] = []
    # Patch mark_judge_ready to count real transitions.
    real = __import__("bot.fights", fromlist=["mark_judge_ready"]).mark_judge_ready

    def counting(db_, fight_id):
        ok = real(db_, fight_id)
        transitions.append(ok)
        return ok

    with patch("bot.fights.mark_judge_ready", side_effect=counting):
        out = rest_fight(db, fid, now=FROZEN + timedelta(minutes=1), actor_id=20)
    assert out["status"] == JUDGE_READY_STATUS
    assert out["rest_a_at"] and out["rest_b_at"]
    assert transitions == [True]
    # Second call / already ready → no further transition.
    assert mark_judge_ready(db, fid) is False
    assert rest_fight(db, fid, now=FROZEN + timedelta(minutes=2), actor_id=10)[
        "status"
    ] == JUDGE_READY_STATUS
    db.close()


def test_a3_both_rest_one_judge_ready_event(tmp_path: Path) -> None:
    """Lead lock A3: near-simultaneous rests → one judge-ready transition."""
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    events: list[str] = []

    real = __import__("bot.fights", fromlist=["mark_judge_ready"]).mark_judge_ready

    def counting(db_, fight_id):
        ok = real(db_, fight_id)
        if ok:
            events.append("judge_ready")
        return ok

    with patch("bot.fights.mark_judge_ready", side_effect=counting):
        rest_fight(db, fid, now=FROZEN, actor_id=10)
        rest_fight(db, fid, now=FROZEN, actor_id=20)
        # Spurious third attempt must not fire again.
        mark_judge_ready(db, fid)

    assert events == ["judge_ready"]
    assert db.get_fight(fid)["status"] == JUDGE_READY_STATUS
    db.close()


def test_judge_due_rests_frozen_clock(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    rest_fight(db, fid, now=FROZEN, actor_id=10)
    assert db.get_fight(fid)["status"] == "resting"
    # Before deadline — no transition.
    assert judge_due_rests(db, FROZEN + timedelta(hours=23)) == []
    assert db.get_fight(fid)["status"] == "resting"
    # Past deadline — single judge_ready.
    ready = judge_due_rests(db, FROZEN + timedelta(hours=24, seconds=1))
    assert ready == [fid]
    assert db.get_fight(fid)["status"] == JUDGE_READY_STATUS
    assert judge_due_rests(db, FROZEN + timedelta(hours=25)) == []
    db.close()


def test_forfeit_confirmation_sets_forfeited(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    out = forfeit_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    assert out["status"] == "forfeited"
    db.close()


def test_forfeit_confirm_view_discord(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _arguing(db)
    from bot.commands import ForfeitConfirmView

    view = ForfeitConfirmView(int(fight["id"]), actor_id=10)
    interaction = MagicMock()
    interaction.user.id = 10
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()

    button = next(
        c for c in view.children if getattr(c, "label", None) == "Confirm forfeit"
    )

    async def _run() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await button.callback(interaction)

    asyncio.run(_run())
    interaction.response.edit_message.assert_awaited()
    assert db.get_fight(fight["id"])["status"] == "forfeited"
    db.close()


def test_cancel_handshake_voided(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    first = cancel_fight(db, fid, now=FROZEN, actor_id=10)
    assert first["status"] == "arguing"
    assert int(first["cancel_requested_by"]) == 10
    second = cancel_fight(db, fid, now=FROZEN + timedelta(seconds=1), actor_id=20)
    assert second["status"] == "voided"
    db.close()


def test_rest_non_advocate_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    with pytest.raises(ValueError, match="advocates"):
        rest_fight(db, int(fight["id"]), now=FROZEN, actor_id=99)
    db.close()


def test_rest_cmd_in_thread_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _arguing(db, thread_id=7777)

    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)
    interaction = MagicMock()
    interaction.user.id = 10
    interaction.channel = MagicMock()
    interaction.channel.id = 7777
    interaction.channel.type = discord.ChannelType.public_thread
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    async def _run() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await FightCog.rest_cmd.callback(cog, interaction)

    asyncio.run(_run())
    interaction.response.send_message.assert_awaited()
    row = db.get_fight(fight["id"])
    assert row["status"] == "resting"
    assert row["rest_a_at"]
    db.close()


def test_same_side_double_rest_rejected(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    rest_fight(db, fid, now=FROZEN, actor_id=10)
    with pytest.raises(ValueError, match="already rested"):
        rest_fight(db, fid, now=FROZEN + timedelta(minutes=1), actor_id=10)
    db.close()
