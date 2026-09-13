"""V2 item 4b — Counter matrix, open-ended accept, counter limits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.fights import (
    accept_fight,
    apply_balance_to_fight,
    button_holder_id,
    counter_button_label,
    counter_fight,
    create_proposed_fight,
    fill_open_ended_accept,
    is_balance_free_counter_eligible,
    sides_complete,
    to_iso,
    validate_fight_fields,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _proposed(db: CourtDB, **kwargs):
    defaults = dict(
        guild_id=1,
        channel_id=2,
        challenger_id=10,  # Matt
        challengee_id=20,  # Renee — button holder
        side_a="Aragorn",
        side_b="Goku",
        context=None,
        now=FROZEN,
        open_ended=False,
    )
    defaults.update(kwargs)
    return create_proposed_fight(db, **defaults)


def test_validate_open_ended_proposed() -> None:
    plan = validate_fight_fields(opponent_id=20, side="Aragorn", context="alley")
    assert plan.kind == "proposed"
    assert plan.open_ended is True
    assert plan.side_a == "Aragorn"
    assert plan.side_b is None
    assert plan.context == "alley"


def test_counter_flips_button_holder(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _proposed(db)
    assert button_holder_id(fight) == 20

    out = counter_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=1), actor_id=20
    )
    assert out["status"] == "proposed"
    assert button_holder_id(out) == 10  # flipped to Matt
    assert out["challenger_id"] == 20
    assert out["challengee_id"] == 10
    assert out["counters_b"] == 1  # Renee was advocate B
    assert out["counters_a"] == 0
    assert out["side_a"] == "Aragorn" and out["side_b"] == "Goku"
    assert out["advocate_a_id"] == 10 and out["advocate_b_id"] == 20
    # Deadline reset
    assert out["expires_at"] > fight["expires_at"] or out["expires_at"] != fight[
        "expires_at"
    ]
    db.close()


def test_counter_swap_sides_and_advocates(tmp_path: Path) -> None:
    """Q1: swap flips side_a↔side_b AND advocate_a↔advocate_b; holder still flips."""
    db = _db(tmp_path)
    fight = _proposed(db)
    out = counter_fight(
        db,
        fight["id"],
        now=FROZEN + timedelta(minutes=2),
        actor_id=20,
        context="no ki",
        swap_sides=True,
    )
    assert out["status"] == "proposed"
    assert out["context"] == "no ki"
    assert out["counters_b"] == 1
    assert button_holder_id(out) == 10
    # After swap: Renee=A(Goku), Matt=B(Aragorn)
    assert out["side_a"] == "Goku"
    assert out["side_b"] == "Aragorn"
    assert out["advocate_a_id"] == 20
    assert out["advocate_b_id"] == 10
    db.close()


def test_counter_nonempty_overwrite_only(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _proposed(db, context="alley", side_a="Aragorn", side_b="Goku")
    # Empty matchup/context → keep prior
    out = counter_fight(
        db,
        fight["id"],
        now=FROZEN + timedelta(minutes=1),
        actor_id=20,
        matchup="   ",
        context="",
        swap_sides=False,
    )
    assert out["side_a"] == "Aragorn"
    assert out["side_b"] == "Goku"
    assert out["context"] == "alley"

    # Non-empty matchup overwrites sides; empty context keeps alley
    out2 = counter_fight(
        db,
        out["id"],
        now=FROZEN + timedelta(minutes=2),
        actor_id=10,  # now button holder
        matchup="Batman vs Superman",
        context=None,
    )
    assert out2["side_a"] == "Batman"
    assert out2["side_b"] == "Superman"
    assert out2["context"] == "alley"
    assert out2["open_ended"] is False
    db.close()


def test_counters_exhausted_voids(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.set_guild_config(1, "counters_per_side", "2")
    fight = _proposed(db)
    # Renee counters twice (as B), flipping holder each time — need to get buttons back.
    # Counter 1: Renee (B) → counters_b=1, Matt holds
    c1 = counter_fight(db, fight["id"], now=FROZEN, actor_id=20)
    assert c1["counters_b"] == 1 and button_holder_id(c1) == 10
    # Matt counters once (as A) so Renee gets buttons again
    c_mid = counter_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=1), actor_id=10
    )
    assert c_mid["counters_a"] == 1 and button_holder_id(c_mid) == 20
    # Counter 2: Renee again → counters_b=2
    c2 = counter_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=2), actor_id=20
    )
    assert c2["counters_b"] == 2 and c2["status"] == "proposed"
    # Matt returns buttons to Renee
    counter_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=3), actor_id=10
    )
    # Third Renee counter → voided (exhausted)
    voided = counter_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=4), actor_id=20
    )
    assert voided["status"] == "voided"
    assert int(voided["counters_b"]) == 2  # not incremented past void
    db.close()


def test_open_ended_accept_fills_side_b(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Aragorn",
        side_b=None,
        context="no prep",
        now=FROZEN,
        open_ended=True,
    )
    assert fight["open_ended"] is True
    assert not sides_complete(fight)
    with pytest.raises(ValueError, match="champion"):
        accept_fight(db, fight["id"], now=FROZEN, actor_id=20)

    filled = fill_open_ended_accept(
        db,
        fight["id"],
        now=FROZEN,
        actor_id=20,
        champion="Goku",
        context="random alley",
    )
    assert filled["side_a"] == "Aragorn"
    assert filled["side_b"] == "Goku"
    assert filled["context"] == "random alley"
    assert sides_complete(filled)

    calls: list[tuple] = []

    def fake_balance(a, b, context=None, **kwargs):
        calls.append((a, b, context))
        return {
            "score": 7.0,
            "favored_side": "b",
            "reason": "ki advantage",
            "franchise_a": "lotr",
            "franchise_b": "dragon_ball",
        }

    bal = apply_balance_to_fight(db, fight["id"], balance_fn=fake_balance)
    assert bal is not None
    assert calls == [("Aragorn", "Goku", "random alley")]
    row = db.get_fight(fight["id"])
    assert row["balance_score"] == 7.0
    assert row["balance_favored"] == "b"
    assert row["balance_warned"] is False  # score 7 is not < default 4

    out = accept_fight(
        db, fight["id"], now=FROZEN + timedelta(minutes=1), actor_id=20, thread_id=42
    )
    assert out["status"] == "arguing"
    assert out["side_b"] == "Goku"
    assert out["thread_id"] == 42
    db.close()


def test_counter_button_label_hook_and_free_flag() -> None:
    assert counter_button_label(None) == "Counter"
    assert counter_button_label({"balance_warned": False}) == "Counter"
    # Explicit flag on the fight (no db) — 4c implements the hook.
    assert (
        counter_button_label({"balance_warned": True, "balance_free_counter": True})
        == "Counter (free)"
    )
    assert is_balance_free_counter_eligible(
        {"balance_warned": True, "balance_free_counter": True}
    ) is True
    assert is_balance_free_counter_eligible(
        {"balance_warned": True, "balance_free_counter": False}
    ) is False
    assert is_balance_free_counter_eligible({"balance_warned": False}) is False


def test_counter_and_open_ended_handlers_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)

    fight = _proposed(db)
    from bot.commands import ChallengeCardView, CounterModal

    view = ChallengeCardView(int(fight["id"]))
    custom_ids = {item.custom_id for item in view.children}
    assert f"fightclub:counter:{fight['id']}" in custom_ids

    # Counter button opens modal for holder
    interaction = MagicMock()
    interaction.user.id = 20
    interaction.response = MagicMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    async def _counter() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await view.on_counter(interaction)

    asyncio.run(_counter())
    interaction.response.send_modal.assert_awaited()
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, CounterModal)

    # Modal submit with swap
    modal_interaction = MagicMock()
    modal_interaction.user.id = 20
    modal_interaction.client = MagicMock()
    modal_interaction.client.add_view = MagicMock()
    modal_interaction.response = MagicMock()
    modal_interaction.response.send_message = AsyncMock()
    modal_interaction.response.edit_message = AsyncMock()
    # Simulate Label→component values (TextInput.value is read-only)
    modal.matchup.component = MagicMock(value="")
    modal.context.component = MagicMock(value="no ki")
    modal.swap.component = MagicMock(value=True)

    async def _submit() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN + timedelta(minutes=1)):
            with patch("bot.commands.apply_balance_to_fight", return_value=None):
                await modal.on_submit(modal_interaction)

    asyncio.run(_submit())
    modal_interaction.response.edit_message.assert_awaited()
    row = db.get_fight(fight["id"])
    assert row["context"] == "no ki"
    assert row["side_a"] == "Goku"
    assert button_holder_id(row) == 10

    # Open-ended accept modal path
    oe = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Aragorn",
        side_b=None,
        context=None,
        now=FROZEN,
        open_ended=True,
    )
    view2 = ChallengeCardView(int(oe["id"]))
    accept_ix = MagicMock()
    accept_ix.user.id = 20
    accept_ix.response = MagicMock()
    accept_ix.response.send_modal = AsyncMock()

    async def _accept_oe() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await view2.on_accept(accept_ix)

    asyncio.run(_accept_oe())
    accept_ix.response.send_modal.assert_awaited()
    from bot.commands import OpenEndedAcceptModal

    oe_modal = accept_ix.response.send_modal.await_args.args[0]
    assert isinstance(oe_modal, OpenEndedAcceptModal)

    oe_modal.champion.component = MagicMock(value="Goku")
    oe_modal.context.component = MagicMock(value="")
    submit_ix = MagicMock()
    submit_ix.user.id = 20
    submit_ix.message = MagicMock()
    submit_ix.message.create_thread = AsyncMock(return_value=MagicMock(id=777))
    submit_ix.response = MagicMock()
    submit_ix.response.is_done = MagicMock(return_value=False)
    submit_ix.response.send_message = AsyncMock()
    submit_ix.response.edit_message = AsyncMock()

    async def _oe_submit() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            with patch(
                "bot.commands.apply_balance_to_fight",
                return_value={"score": 5.0, "favored_side": "even", "reason": "x"},
            ):
                await oe_modal.on_submit(submit_ix)

    asyncio.run(_oe_submit())
    done = db.get_fight(oe["id"])
    assert done["status"] == "arguing"
    assert done["side_b"] == "Goku"
    assert done["thread_id"] == 777
    db.close()
