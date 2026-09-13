"""V2 item 9 — derived records, leaderboard, flare."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.embeds import ruling_drop_embed
from bot.fights import accept_fight, create_proposed_fight, forfeit_fight, to_iso
from bot.judge import validate_verdict
from bot.records import (
    compute_streak,
    format_flare_line,
    format_last5,
    format_streak_label,
    iter_guild_outcomes,
    leaderboard,
    record_for_user,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _proposed(
    db: CourtDB,
    *,
    guild_id: int = 1,
    a: int = 10,
    b: int = 20,
    side_a: str = "Goku",
    side_b: str = "Vegeta",
    instant: bool = False,
) -> dict:
    fight = create_proposed_fight(
        db,
        guild_id=guild_id,
        channel_id=2,
        challenger_id=a,
        challengee_id=b,
        side_a=side_a,
        side_b=side_b,
        context="test",
        now=FROZEN,
    )
    if instant:
        db.update_fight(int(fight["id"]), instant=True)
        fight = db.get_fight(int(fight["id"]))
        assert fight is not None
    return fight


def _arguing(db: CourtDB, **kw: Any) -> dict:
    fight = _proposed(db, **kw)
    fid = int(fight["id"])
    accept_fight(db, fid, now=FROZEN, actor_id=int(fight["challengee_id"]), thread_id=9000 + fid)
    return db.get_fight(fid)  # type: ignore[return-value]


def _insert_ruling(
    db: CourtDB,
    fight: dict,
    *,
    winner_side: str,
    kind: str = "initial",
    ruled_at: str | None = None,
) -> int:
    fid = int(fight["id"])
    winner_adv = int(fight[f"advocate_{winner_side}_id"])
    rid = db.insert_ruling(
        message_id=None,
        channel_id=int(fight.get("thread_id") or 0) or None,
        guild_id=int(fight["guild_id"]),
        fighter_a=str(fight.get("side_a") or "A"),
        fighter_b=str(fight.get("side_b") or "B"),
        context=fight.get("context"),
        verdict={"winner_side": winner_side, "ruling": "x", "confidence": 7},
        fight_id=fid,
        kind=kind,
        winner_side=winner_side,
        winner_advocate_id=winner_adv,
        confidence=7.0,
    )
    iso = ruled_at or to_iso(FROZEN)
    db.update_fight(fid, status="ruled", ruled_at=iso)
    return rid


# --- Streak helper -----------------------------------------------------------


def test_compute_streak_cases() -> None:
    assert compute_streak([]) == (0, "")
    assert compute_streak(["W"]) == (1, "W")
    assert compute_streak(["L", "L", "L"]) == (3, "L")
    assert compute_streak(["W", "W", "L", "W", "W"]) == (2, "W")
    assert compute_streak(["W", "W", "W", "L"]) == (1, "L")
    assert compute_streak(["L", "W"]) == (1, "W")


def test_format_flare_string() -> None:
    assert (
        format_flare_line("Matt", 5, 2, 3, "W")
        == "Matt moves to 5-2, 3-fight win streak."
    )
    assert (
        format_flare_line("Matt", 5, 2, 2, "L")
        == "Matt moves to 5-2, 2-fight loss streak."
    )
    assert format_flare_line("Ada", 1, 0, 1, "W") == (
        "Ada moves to 1-0, 1-fight win streak."
    )
    assert format_streak_label(3, "W") == "3W"
    assert format_last5(("W", "L", "W")) == "W L W"


# --- Derived W/L -------------------------------------------------------------


def test_forfeit_counts_as_l_for_forfeiter(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db, a=10, b=20)
    out = forfeit_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    assert out["status"] == "forfeited"
    assert int(out["forfeited_by"]) == 10
    assert out["forfeited_at"]

    rec10 = record_for_user(db, 1, 10)
    rec20 = record_for_user(db, 1, 20)
    assert (rec10.wins, rec10.losses) == (0, 1)
    assert (rec20.wins, rec20.losses) == (1, 0)
    assert rec10.streak_kind == "L"
    assert rec20.streak_kind == "W"
    db.close()


def test_voided_fight_excluded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db, a=10, b=20)
    _insert_ruling(db, fight, winner_side="a")
    db.update_fight(int(fight["id"]), status="voided")
    assert record_for_user(db, 1, 10).wins == 0
    assert record_for_user(db, 1, 20).losses == 0
    assert iter_guild_outcomes(db, 1) == []
    db.close()


def test_instant_fight_excluded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _proposed(db, a=10, b=20, instant=True)
    # Instant never goes through argue; stamp a ruling-like row anyway.
    db.update_fight(int(fight["id"]), status="ruled", ruled_at=to_iso(FROZEN))
    db.insert_ruling(
        message_id=None,
        channel_id=2,
        guild_id=1,
        fighter_a="Goku",
        fighter_b="Vegeta",
        context=None,
        verdict={"winner_side": "a"},
        fight_id=int(fight["id"]),
        kind="initial",
        winner_side="a",
        winner_advocate_id=10,
    )
    assert iter_guild_outcomes(db, 1) == []
    # kind=instant also excluded even if instant flag cleared
    fight2 = _arguing(db, a=10, b=20)
    db.update_fight(int(fight2["id"]), instant=False, status="ruled", ruled_at=to_iso(FROZEN))
    db.insert_ruling(
        message_id=None,
        channel_id=2,
        guild_id=1,
        fighter_a="A",
        fighter_b="B",
        context=None,
        verdict={},
        fight_id=int(fight2["id"]),
        kind="instant",
        winner_side="a",
        winner_advocate_id=10,
    )
    assert iter_guild_outcomes(db, 1) == []
    db.close()


def test_latest_reconsideration_wins(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db, a=10, b=20)
    _insert_ruling(
        db, fight, winner_side="a", kind="initial", ruled_at=to_iso(FROZEN)
    )
    # Reconsideration reverses winner.
    db.insert_ruling(
        message_id=None,
        channel_id=2,
        guild_id=1,
        fighter_a="Goku",
        fighter_b="Vegeta",
        context=None,
        verdict={"winner_side": "b"},
        fight_id=int(fight["id"]),
        kind="reconsideration",
        winner_side="b",
        winner_advocate_id=20,
        confidence=6.0,
    )
    # Keep fight ruled; bump ruled_at.
    db.update_fight(
        int(fight["id"]),
        status="ruled",
        ruled_at=to_iso(FROZEN + timedelta(hours=1)),
    )
    rec10 = record_for_user(db, 1, 10)
    rec20 = record_for_user(db, 1, 20)
    assert (rec10.wins, rec10.losses) == (0, 1)
    assert (rec20.wins, rec20.losses) == (1, 0)
    outcomes = iter_guild_outcomes(db, 1)
    assert len(outcomes) == 1
    assert outcomes[0].winner_id == 20
    db.close()


def test_expired_excluded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db, a=10, b=20)
    _insert_ruling(db, fight, winner_side="a")
    db.update_fight(int(fight["id"]), status="expired")
    assert iter_guild_outcomes(db, 1) == []
    db.close()


def test_streak_across_mixed_results(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # User 10: W, W, L, W, W → streak 2W, record 4-1
    times = [FROZEN + timedelta(hours=i) for i in range(5)]
    winners = ["a", "a", "b", "a", "a"]  # a=10, b=20
    for i, (t, w) in enumerate(zip(times, winners)):
        fight = _arguing(db, a=10, b=20)
        _insert_ruling(db, fight, winner_side=w, ruled_at=to_iso(t))
    rec = record_for_user(db, 1, 10)
    assert (rec.wins, rec.losses) == (4, 1)
    assert rec.streak == 2 and rec.streak_kind == "W"
    assert rec.last5 == ("W", "W", "L", "W", "W")
    db.close()


def test_leaderboard_ordering(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # User 10: 3-0; User 30: 2-0; User 20: 0-3 (opponent of 10)
    # User 40: 2-2 (same wins as 30 but lower win%)
    for _ in range(3):
        f = _arguing(db, a=10, b=20)
        _insert_ruling(db, f, winner_side="a", ruled_at=to_iso(FROZEN))
    for _ in range(2):
        f = _arguing(db, a=30, b=50)
        _insert_ruling(db, f, winner_side="a", ruled_at=to_iso(FROZEN))
    for _ in range(2):
        f = _arguing(db, a=40, b=60)
        _insert_ruling(db, f, winner_side="a", ruled_at=to_iso(FROZEN))
    for _ in range(2):
        f = _arguing(db, a=40, b=70)
        _insert_ruling(db, f, winner_side="b", ruled_at=to_iso(FROZEN))

    board = leaderboard(db, 1, limit=15)
    # 10: 3-0; 30 & 70: 2-0 (user_id tiebreak → 30 then 70); 40: 2-2
    assert [r.user_id for r in board[:4]] == [10, 30, 70, 40]
    assert board[0].wins == 3
    assert board[1].wins == 2 and board[1].win_pct == 1.0
    assert board[2].wins == 2 and board[2].win_pct == 1.0
    assert board[3].wins == 2 and board[3].win_pct == 0.5
    # streak column present
    assert board[0].streak >= 1
    db.close()


def test_ruling_drop_embed_appends_flare() -> None:
    v = validate_verdict(
        {
            "steelman_a": "A",
            "steelman_b": "B",
            "ruling": "A wins",
            "winner_side": "a",
            "confidence": 7,
            "citations": [],
        }
    )
    flare = format_flare_line("Matt", 5, 2, 3, "W")
    embed = ruling_drop_embed(
        v,
        fight={"side_a": "Goku", "side_b": "Vegeta"},
        flare_line=flare,
    )
    values = [f.value for f in embed.fields]
    assert any("Matt moves to 5-2, 3-fight win streak." in (val or "") for val in values)


def test_drop_ruling_messages_wires_flare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _arguing(db, a=10, b=20)
    _insert_ruling(db, fight, winner_side="a")
    fight = db.get_fight(int(fight["id"]))
    assert fight is not None

    from bot.commands import drop_ruling_messages

    thread = MagicMock()
    thread.id = fight["thread_id"]
    thread_msg = MagicMock()
    thread_msg.id = 555
    thread.send = AsyncMock(return_value=thread_msg)
    parent = MagicMock()
    parent.send = AsyncMock()

    result = {
        "fight": fight,
        "verdict": {"winner_side": "a", "ruling": "x", "confidence": 7, "citations": []},
        "thin_record": False,
        "exhibit_ledger": [],
        "ruling_id": None,
    }

    async def _run() -> None:
        await drop_ruling_messages(
            thread=thread,
            parent_channel=parent,
            result=result,
            winner_display="Alice",
        )

    asyncio.run(_run())
    thread.send.assert_awaited()
    embed = thread.send.await_args.kwargs.get("embed") or thread.send.await_args.args[0]
    # After the ruling, Alice (advocate a=10) is 1-0 with 1W streak.
    values = [f.value for f in embed.fields]
    assert any("Alice moves to 1-0, 1-fight win streak." in (v or "") for v in values)
    db.close()


def test_leaderboard_cmd_mock_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    f = _arguing(db, a=10, b=20)
    _insert_ruling(db, f, winner_side="a")

    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)
    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.guild = MagicMock()
    interaction.guild.get_member.return_value = None
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    asyncio.run(cog.leaderboard_cmd.callback(cog, interaction))
    interaction.response.send_message.assert_awaited()
    kw = interaction.response.send_message.await_args.kwargs
    embed = kw.get("embed")
    assert embed is not None
    assert "Leaderboard" in embed.title
    db.close()


def test_record_cmd_mock_discord(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    f = _arguing(db, a=10, b=20)
    _insert_ruling(db, f, winner_side="a")

    from bot.commands import FightCog

    cog = FightCog.__new__(FightCog)
    user = MagicMock()
    user.id = 10
    user.display_name = "Alice"
    interaction = MagicMock()
    interaction.guild_id = 1
    interaction.user = user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    asyncio.run(cog.record_cmd.callback(cog, interaction, user=None))
    interaction.response.send_message.assert_awaited()
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "1-0" in (embed.description or "")
    db.close()
