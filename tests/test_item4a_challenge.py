"""V2 item 4a — challenge card Accept/Decline + expire_due_fights."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.embeds import challenge_card_embed
from bot.fights import (
    MISSING_FIGHT_PROMPT,
    accept_fight,
    challenge_timeout_hours,
    compute_expires_at,
    create_proposed_fight,
    decline_fight,
    expire_due_fights,
    parse_matchup,
    resolve_sides,
    to_iso,
    validate_fight_fields,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def test_parse_matchup_and_resolve_sides() -> None:
    assert parse_matchup("Aragorn vs Goku") == ("Aragorn", "Goku")
    assert parse_matchup("A VS. B") == ("A", "B")
    assert parse_matchup("solo") is None
    assert resolve_sides("Aragorn vs Goku", None) == ("Aragorn", "Goku")
    assert resolve_sides("Aragorn vs Goku", "B") == ("Goku", "Aragorn")
    assert resolve_sides("Aragorn vs Goku", "Goku") == ("Goku", "Aragorn")
    assert resolve_sides("Aragorn vs Goku", "A") == ("Aragorn", "Goku")


def test_validate_fight_fields_ephemeral_prompt_when_missing() -> None:
    plan = validate_fight_fields()
    assert plan.kind == "prompt"
    assert plan.prompt == MISSING_FIGHT_PROMPT

    plan2 = validate_fight_fields(matchup="A vs B")  # no opponent, not instant
    assert plan2.kind == "prompt"

    plan3 = validate_fight_fields(opponent_id=99, side="Aragorn")  # open-ended (4b)
    assert plan3.kind == "proposed"
    assert plan3.open_ended is True
    assert plan3.side_a == "Aragorn"
    assert plan3.side_b is None


def test_validate_fight_fields_proposed_and_instant() -> None:
    # V2.1 shape A — champion_a / champion_b
    prop = validate_fight_fields(
        opponent_id=2,
        champion_a="Goku",
        champion_b="Aragorn",
        context="no ki",
    )
    assert prop.kind == "proposed"
    assert prop.side_a == "Goku"
    assert prop.side_b == "Aragorn"
    assert prop.context == "no ki"
    assert prop.open_ended is False

    inst = validate_fight_fields(
        instant=True, champion_a="A", champion_b="B"
    )
    assert inst.kind == "instant"
    assert inst.side_a == "A" and inst.side_b == "B"

    open_ended = validate_fight_fields(opponent_id=99, champion_a="Aragorn")
    assert open_ended.kind == "proposed" and open_ended.open_ended is True

    # Legacy aliases still accepted by the helper (slash command dropped them).
    legacy = validate_fight_fields(fighter_a="X", fighter_b="Y")
    assert legacy.kind == "instant"
    assert legacy.side_a == "X" and legacy.side_b == "Y"
    prop_legacy = validate_fight_fields(
        opponent_id=2, matchup="Aragorn vs Goku", side="B"
    )
    assert prop_legacy.side_a == "Goku" and prop_legacy.side_b == "Aragorn"


def test_challenge_timeout_hours_guild_overrides_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setenv("FIGHT_CHALLENGE_TIMEOUT_HOURS", "3")
    assert challenge_timeout_hours(db, guild_id=None) == 3.0
    db.set_guild_config(7, "challenge_timeout_hours", "9")
    assert challenge_timeout_hours(db, guild_id=7) == 9.0
    db.close()


def test_expire_due_fights_with_frozen_clock(tmp_path: Path) -> None:
    db = _db(tmp_path)
    due = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN - timedelta(hours=7),
    )
    alive = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=11,
        challengee_id=21,
        side_a="C",
        side_b="D",
        context=None,
        now=FROZEN,
    )
    # Force due row's expires_at into the past relative to FROZEN.
    db.update_fight(due["id"], expires_at=to_iso(FROZEN - timedelta(minutes=1)))
    db.update_fight(alive["id"], expires_at=to_iso(FROZEN + timedelta(hours=6)))

    expired = expire_due_fights(db, FROZEN)
    assert due["id"] in expired
    assert alive["id"] not in expired
    assert db.get_fight(due["id"])["status"] == "expired"
    assert db.get_fight(alive["id"])["status"] == "proposed"

    # Idempotent
    assert expire_due_fights(db, FROZEN) == []
    db.close()


def test_accept_transitions_to_arguing_with_accepted_at(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Aragorn",
        side_b="Goku",
        context="no prep",
        now=FROZEN,
    )
    assert fight["status"] == "proposed"
    assert fight["expires_at"] == compute_expires_at(FROZEN, 6.0)

    with pytest.raises(ValueError, match="challenged"):
        accept_fight(db, fight["id"], now=FROZEN, actor_id=10)

    out = accept_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=5), actor_id=20, thread_id=999
    )
    assert out["status"] in {"accepted", "arguing"}
    assert out["status"] == "arguing"
    assert out["accepted_at"] == to_iso(FROZEN + timedelta(minutes=5))
    assert out["thread_id"] == 999
    db.close()


def test_decline_voids_proposed(tmp_path: Path) -> None:
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
    with pytest.raises(ValueError, match="challenged"):
        decline_fight(db, fight["id"], now=FROZEN, actor_id=10)
    out = decline_fight(db, fight["id"], now=FROZEN, actor_id=20)
    assert out["status"] == "voided"
    db.close()


def test_accept_after_expiry_rejected(tmp_path: Path) -> None:
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
    db.update_fight(fight["id"], expires_at=to_iso(FROZEN - timedelta(seconds=1)))
    with pytest.raises(ValueError, match="expired"):
        accept_fight(db, fight["id"], now=FROZEN, actor_id=20)
    assert db.get_fight(fight["id"])["status"] == "expired"
    db.close()


def test_challenge_card_embed_and_view_custom_ids(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Aragorn",
        side_b="Goku",
        context="alley",
        now=FROZEN,
    )
    embed = challenge_card_embed(fight)
    assert "Aragorn" in embed.title
    assert embed.fields  # has side fields

    from bot.commands import ChallengeCardView

    view = ChallengeCardView(int(fight["id"]))
    custom_ids = {item.custom_id for item in view.children}
    assert f"fightclub:accept:{fight['id']}" in custom_ids
    assert f"fightclub:decline:{fight['id']}" in custom_ids
    assert f"fightclub:counter:{fight['id']}" in custom_ids
    db.close()


def test_challenge_card_accept_decline_handlers_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Button handlers with mocked Interaction — no live gateway."""
    import asyncio

    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)

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

    from bot.commands import ChallengeCardView

    view = ChallengeCardView(int(fight["id"]))

    interaction = MagicMock()
    interaction.user.id = 20
    interaction.message = MagicMock()
    interaction.message.create_thread = AsyncMock(return_value=MagicMock(id=555))
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()

    async def _run_accept() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await view.on_accept(interaction)

    asyncio.run(_run_accept())

    interaction.response.edit_message.assert_awaited()
    row = db.get_fight(fight["id"])
    assert row["status"] == "arguing"
    assert row["accepted_at"] == to_iso(FROZEN)
    assert row["thread_id"] == 555

    fight2 = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="C",
        side_b="D",
        context=None,
        now=FROZEN,
    )
    view2 = ChallengeCardView(int(fight2["id"]))
    interaction2 = MagicMock()
    interaction2.user.id = 20
    interaction2.response = MagicMock()
    interaction2.response.send_message = AsyncMock()
    interaction2.response.edit_message = AsyncMock()

    async def _run_decline() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await view2.on_decline(interaction2)

    asyncio.run(_run_decline())
    assert db.get_fight(fight2["id"])["status"] == "voided"
    db.close()
