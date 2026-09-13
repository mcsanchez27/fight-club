"""V2 item 12 — instant:true on fight rows; Challenge limited to instant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.fights import (
    RULED_STATUS,
    accept_fight,
    create_instant_fight,
    create_proposed_fight,
    to_iso,
)
from bot.judge import validate_verdict
from bot.records import iter_guild_outcomes, record_for_user
from bot.ruling import (
    CHALLENGE_ONLY_INSTANT_MSG,
    challenge_allowed_for_ruling,
    finalize_instant_ruling,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _verdict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "steelman_a": "A scales.",
        "steelman_b": "B tanks.",
        "exhibit_ledger": [],
        "concessions": [],
        "unknowns": [],
        "opening_score_a": None,
        "opening_score_b": None,
        "close_score_a": None,
        "close_score_b": None,
        "ruling": "A takes it.",
        "winner_side": "a",
        "winner": "Aragorn",
        "confidence": 7.0,
        "argument_quality": None,
        "citations": [],
        "matchup": "Aragorn vs Goku",
        "retrieval_status": "ok",
    }
    base.update(overrides)
    return validate_verdict(base)


def test_create_instant_fight_row_no_thread(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_instant_fight(
        db,
        guild_id=1,
        channel_id=2,
        user_id=99,
        side_a="Aragorn",
        side_b="Goku",
        context="no ki",
        now=FROZEN,
    )
    assert bool(fight["instant"]) is True
    assert fight["status"] == "proposed"
    assert fight["thread_id"] is None
    assert fight["side_a"] == "Aragorn"
    assert fight["side_b"] == "Goku"
    assert fight["advocate_a_id"] == 99
    assert fight["challengee_id"] is None
    db.close()


def test_finalize_instant_ruling_proposed_to_ruled(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_instant_fight(
        db,
        guild_id=1,
        channel_id=2,
        user_id=99,
        side_a="Aragorn",
        side_b="Goku",
        context=None,
        now=FROZEN,
    )
    out = finalize_instant_ruling(
        db,
        int(fight["id"]),
        _verdict(),
        message_id=555,
        channel_id=2,
        guild_id=1,
        now=FROZEN,
    )
    assert out["kind"] == "instant"
    fight2 = db.get_fight(int(fight["id"]))
    assert fight2 is not None
    assert fight2["status"] == RULED_STATUS
    assert fight2["thread_id"] is None
    assert bool(fight2["instant"]) is True
    assert fight2["ruled_at"] == to_iso(FROZEN)

    ruling = db.get_ruling_by_id(int(out["ruling_id"]))
    assert ruling is not None
    assert ruling["kind"] == "instant"
    assert ruling["fight_id"] == int(fight["id"])
    assert ruling["message_id"] == 555
    assert ruling["winner_side"] == "a"
    assert ruling["parent_ruling_id"] is None
    db.close()


def test_instant_excluded_from_derived_records(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_instant_fight(
        db,
        guild_id=1,
        channel_id=2,
        user_id=10,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN,
    )
    finalize_instant_ruling(
        db,
        int(fight["id"]),
        _verdict(winner_side="a", winner="A"),
        message_id=1,
        now=FROZEN,
    )
    assert iter_guild_outcomes(db, 1) == []
    assert record_for_user(db, 1, 10).wins == 0
    assert record_for_user(db, 1, 10).losses == 0
    db.close()


def test_challenge_allowed_only_for_instant(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Instant ruling → allowed
    inst = create_instant_fight(
        db,
        guild_id=1,
        channel_id=2,
        user_id=10,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN,
    )
    out = finalize_instant_ruling(
        db, int(inst["id"]), _verdict(), message_id=10, now=FROZEN
    )
    ruling = db.get_ruling_by_id(int(out["ruling_id"]))
    ok, err = challenge_allowed_for_ruling(ruling, fight=db.get_fight(int(inst["id"])))
    assert ok is True
    assert err is None

    # Thread initial ruling → rejected (point to reconsider)
    prop = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Goku",
        side_b="Vegeta",
        context="x",
        now=FROZEN,
    )
    fid = int(prop["id"])
    accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=9001)
    db.update_fight(fid, status=RULED_STATUS, ruled_at=to_iso(FROZEN))
    rid = db.insert_ruling(
        message_id=20,
        channel_id=9001,
        guild_id=1,
        fighter_a="Goku",
        fighter_b="Vegeta",
        context="x",
        verdict={"winner_side": "a"},
        fight_id=fid,
        kind="initial",
        winner_side="a",
        winner_advocate_id=10,
    )
    thread_ruling = db.get_ruling_by_id(rid)
    fight = db.get_fight(fid)
    ok2, err2 = challenge_allowed_for_ruling(thread_ruling, fight=fight)
    assert ok2 is False
    assert err2 == CHALLENGE_ONLY_INSTANT_MSG
    assert "/reconsider" in (err2 or "")

    # Legacy V1 (no fight_id / kind) still allowed
    ok3, err3 = challenge_allowed_for_ruling(
        {"verdict": {}, "kind": None, "fight_id": None}
    )
    assert ok3 is True
    assert err3 is None
    db.close()


def test_challenge_view_rejects_non_instant(tmp_path: Path) -> None:
    from bot.commands import ChallengeView, _last_verdict, store_ruling

    _last_verdict.clear()
    db = _db(tmp_path)

    prop = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Goku",
        side_b="Vegeta",
        context="x",
        now=FROZEN,
    )
    fid = int(prop["id"])
    accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=9001)
    db.update_fight(fid, status=RULED_STATUS, ruled_at=to_iso(FROZEN))
    rid = db.insert_ruling(
        message_id=42,
        channel_id=9001,
        guild_id=1,
        fighter_a="Goku",
        fighter_b="Vegeta",
        context="x",
        verdict={"winner_side": "a", "ruling": "ok"},
        fight_id=fid,
        kind="initial",
        winner_side="a",
        winner_advocate_id=10,
    )
    store_ruling(
        42,
        {"winner_side": "a", "ruling": "ok"},
        fighter_a="Goku",
        fighter_b="Vegeta",
        context="x",
        ruling_id=rid,
        fight_id=fid,
        kind="initial",
    )

    view = ChallengeView()
    interaction = MagicMock()
    interaction.message = MagicMock()
    interaction.message.id = 42
    sent: dict[str, Any] = {}

    async def send_message(content=None, ephemeral=False, **kwargs):
        sent["content"] = content
        sent["ephemeral"] = ephemeral

    interaction.response.send_message = send_message
    interaction.response.send_modal = AsyncMock()

    with patch("bot.commands.get_db", return_value=db):
        asyncio.run(view.challenge.callback(interaction))

    assert sent.get("ephemeral") is True
    assert "/reconsider" in str(sent.get("content") or "")
    interaction.response.send_modal.assert_not_called()
    db.close()
    _last_verdict.clear()


def test_challenge_view_opens_modal_for_instant(tmp_path: Path) -> None:
    from bot.commands import ChallengeView, _last_verdict, store_ruling

    _last_verdict.clear()
    db = _db(tmp_path)
    fight = create_instant_fight(
        db,
        guild_id=1,
        channel_id=2,
        user_id=10,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN,
    )
    out = finalize_instant_ruling(
        db, int(fight["id"]), _verdict(), message_id=77, now=FROZEN
    )
    store_ruling(
        77,
        out["verdict"],
        fighter_a="A",
        fighter_b="B",
        context=None,
        ruling_id=int(out["ruling_id"]),
        fight_id=int(fight["id"]),
        kind="instant",
    )

    view = ChallengeView()
    interaction = MagicMock()
    interaction.message = MagicMock()
    interaction.message.id = 77
    interaction.response.send_message = MagicMock()
    interaction.response.send_modal = AsyncMock()

    with patch("bot.commands.get_db", return_value=db):
        asyncio.run(view.challenge.callback(interaction))

    interaction.response.send_modal.assert_called_once()
    interaction.response.send_message.assert_not_called()
    db.close()
    _last_verdict.clear()


def test_finalize_rejects_non_instant_fight(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN,
    )
    with pytest.raises(ValueError, match="instant"):
        finalize_instant_ruling(
            db, int(fight["id"]), _verdict(), message_id=1, now=FROZEN
        )
    db.close()
