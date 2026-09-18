"""V2 item 4c — balance warning chrome + free counter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.embeds import balance_warning_field, challenge_card_embed
from bot.fights import (
    accept_fight,
    apply_balance_to_fight,
    balance_warn_below,
    counter_button_label,
    counter_fight,
    create_proposed_fight,
    fill_open_ended_accept,
    is_balance_free_counter_eligible,
    sides_complete,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _proposed(db: CourtDB, **kwargs):
    defaults = dict(
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Aragorn",
        side_b="Goku",
        context=None,
        now=FROZEN,
        open_ended=False,
    )
    defaults.update(kwargs)
    return create_proposed_fight(db, **defaults)


def _lopsided(a="Aragorn", b="Goku", context=None, **kwargs):
    out = {
        "score": 3.0,
        "favored_side": "b",
        "reason": "ki outscales swords",
        "franchise_a": "Lord of the Rings",
        "franchise_b": "Dragon Ball",
    }
    out.update(kwargs)
    return out


def _even(**kwargs):
    out = {
        "score": 9.0,
        "favored_side": "even",
        "reason": "peer duel",
        "franchise_a": "lotr",
        "franchise_b": "lotr",
    }
    out.update(kwargs)
    return out


def test_deferred_balance_open_ended_until_both_sides(tmp_path: Path) -> None:
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
    calls: list[tuple] = []

    def fake_balance(a, b, context=None, **kwargs):
        calls.append((a, b, context))
        return _lopsided()

    assert not sides_complete(fight)
    assert apply_balance_to_fight(db, fight["id"], balance_fn=fake_balance) is None
    assert calls == []
    row = db.get_fight(fight["id"])
    assert row["balance_score"] is None
    assert row["balance_warned"] is False
    assert balance_warning_field(row) is None

    filled = fill_open_ended_accept(
        db,
        fight["id"],
        now=FROZEN,
        actor_id=20,
        champion="Goku",
    )
    assert sides_complete(filled)
    bal = apply_balance_to_fight(db, fight["id"], balance_fn=fake_balance)
    assert bal is not None
    assert calls == [("Aragorn", "Goku", "no prep")]
    row = db.get_fight(fight["id"])
    assert row["balance_score"] == 3.0
    assert row["balance_favored"] == "b"
    assert row["balance_reason"] == "ki outscales swords"
    assert row["balance_warned"] is True
    assert row["franchise_a"] == "lotr"
    assert row["franchise_b"] == "dragon_ball"
    warn = balance_warning_field(row)
    assert warn is not None
    assert warn[0] == "⚖️ Referee's read"
    assert "Goku favored (3/10)" in warn[1]
    assert "ki outscales swords" in warn[1]
    db.close()


def test_warning_field_below_threshold_not_when_even(tmp_path: Path) -> None:
    db = _db(tmp_path)
    assert balance_warn_below(db, 1) == 4.0

    low = _proposed(db, side_a="Aragorn", side_b="Goku")
    apply_balance_to_fight(db, low["id"], balance_fn=lambda *a, **k: _lopsided())
    row = db.get_fight(low["id"])
    assert row["balance_warned"] is True
    field = balance_warning_field(row)
    assert field is not None
    name, value = field
    assert name == "⚖️ Referee's read"
    assert "favored (3/10)" in value
    embed = challenge_card_embed(row)
    titles = [f.name for f in embed.fields]
    assert "⚖️ Referee's read" in titles

    # Score equal to threshold is not a warning (strict <).
    eq = _proposed(db, side_a="Frodo", side_b="Sam", challenger_id=11, challengee_id=21)
    apply_balance_to_fight(
        db,
        eq["id"],
        balance_fn=lambda *a, **k: _even(score=4.0, favored_side="a", reason="edge"),
    )
    eq_row = db.get_fight(eq["id"])
    assert eq_row["balance_score"] == 4.0
    assert eq_row["balance_warned"] is False
    assert balance_warning_field(eq_row) is None

    high = _proposed(db, side_a="A", side_b="B", challenger_id=12, challengee_id=22)
    apply_balance_to_fight(db, high["id"], balance_fn=lambda *a, **k: _even())
    hi = db.get_fight(high["id"])
    assert hi["balance_warned"] is False
    assert balance_warning_field(hi) is None
    hi_embed = challenge_card_embed(hi)
    assert "⚖️ Referee's read" not in [f.name for f in hi_embed.fields]
    db.close()


def test_guild_threshold_override(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.set_guild_config(1, "balance_warn_below", "8")
    fight = _proposed(db)
    apply_balance_to_fight(
        db, fight["id"], balance_fn=lambda *a, **k: _even(score=7.0)
    )
    row = db.get_fight(fight["id"])
    assert row["balance_warned"] is True  # 7 < 8
    db.close()


def test_free_counter_skips_quota_normal_still_counts(tmp_path: Path) -> None:
    db = _db(tmp_path)
    db.set_guild_config(1, "balance_free_counter", "1")
    db.set_guild_config(1, "counters_per_side", "2")

    warned = _proposed(db)
    apply_balance_to_fight(
        db, warned["id"], balance_fn=lambda *a, **k: _lopsided()
    )
    row = db.get_fight(warned["id"])
    assert row["balance_warned"] is True
    assert is_balance_free_counter_eligible(row, db=db) is True
    assert counter_button_label(row, db=db) == "Counter (free)"

    out = counter_fight(db, warned["id"], now=FROZEN, actor_id=20)
    assert out["status"] == "proposed"
    assert int(out["counters_a"] or 0) == 0
    assert int(out["counters_b"] or 0) == 0  # free — not incremented

    # Disable free counter: same warned fight now consumes quota.
    db.set_guild_config(1, "balance_free_counter", "0")
    # Holder flipped to 10 after first counter.
    out2 = counter_fight(
        db, warned["id"], now=FROZEN + timedelta(minutes=1), actor_id=10
    )
    assert int(out2["counters_a"] or 0) == 1
    assert int(out2["counters_b"] or 0) == 0

    # Unwarned fight always counts.
    even = _proposed(db, challenger_id=13, challengee_id=23)
    apply_balance_to_fight(db, even["id"], balance_fn=lambda *a, **k: _even())
    even_row = db.get_fight(even["id"])
    assert even_row["balance_warned"] is False
    assert is_balance_free_counter_eligible(even_row, db=db) is False
    assert counter_button_label(even_row, db=db) == "Counter"
    counted = counter_fight(db, even["id"], now=FROZEN, actor_id=23)
    assert counted["counters_b"] == 1
    db.close()


def test_underdog_accepted_on_warned_accept(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _proposed(db)  # A=Aragorn/10, B=Goku/20
    apply_balance_to_fight(
        db, fight["id"], balance_fn=lambda *a, **k: _lopsided()
    )  # favored b → underdog is a (challenger 10)
    # Challengee 20 is the favorite — Accept does not mark underdog_accepted.
    fav = accept_fight(db, fight["id"], now=FROZEN, actor_id=20)
    assert fav["underdog_accepted"] is False

    fight2 = _proposed(db, challenger_id=30, challengee_id=40)
    apply_balance_to_fight(
        db,
        fight2["id"],
        balance_fn=lambda *a, **k: _lopsided(favored_side="a"),
    )  # favored a → underdog is b (challengee 40)
    und = accept_fight(db, fight2["id"], now=FROZEN, actor_id=40)
    assert und["underdog_accepted"] is True
    assert und["balance_warned"] is True
    db.close()


def test_no_one_sided_warning_on_open_card(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = create_proposed_fight(
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
    # Even if someone stamped a warned flag, chrome stays off until both sides.
    db.update_fight(fight["id"], balance_warned=True, balance_score=2.0)
    row = db.get_fight(fight["id"])
    assert balance_warning_field(row) is None
    db.close()


def test_apply_balance_uses_judge_balance_mock(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _proposed(db)
    fake = MagicMock(return_value=_lopsided(score=2.0, reason="stomp"))
    with patch("bot.judge.judge_balance", fake):
        result = apply_balance_to_fight(db, fight["id"])
    fake.assert_called_once()
    assert result["score"] == 2.0
    row = db.get_fight(fight["id"])
    assert row["balance_warned"] is True
    assert row["franchise_a"] == "lotr"
    db.close()


def test_challenge_card_view_free_counter_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _proposed(db)
    apply_balance_to_fight(
        db, fight["id"], balance_fn=lambda *a, **k: _lopsided()
    )
    from bot.commands import ChallengeCardView

    view = ChallengeCardView(int(fight["id"]))
    labels = [getattr(item, "label", None) for item in view.children]
    assert "Counter (free)" in labels
    assert "Accept" in labels
    db.close()


def test_counter_modal_free_does_not_increment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _proposed(db)
    apply_balance_to_fight(
        db, fight["id"], balance_fn=lambda *a, **k: _lopsided()
    )

    from bot.commands import ChallengeCardView, CounterModal

    view = ChallengeCardView(int(fight["id"]))
    interaction = MagicMock()
    interaction.user.id = 20
    interaction.response = MagicMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.send_message = AsyncMock()

    async def _open() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            await view.on_counter(interaction)

    asyncio.run(_open())
    modal = interaction.response.send_modal.await_args.args[0]
    assert isinstance(modal, CounterModal)

    modal.matchup.component = MagicMock(value="")
    modal.context.component = MagicMock(value="")
    modal.swap.component = MagicMock(value=False)
    submit = MagicMock()
    submit.user.id = 20
    submit.client = MagicMock()
    submit.client.add_view = MagicMock()
    submit.response = MagicMock()
    submit.response.send_message = AsyncMock()
    submit.response.edit_message = AsyncMock()

    async def _submit() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN + timedelta(minutes=1)):
            with patch(
                "bot.commands.apply_balance_to_fight",
                return_value=_lopsided(),
            ):
                await modal.on_submit(submit)

    asyncio.run(_submit())
    submit.response.edit_message.assert_awaited()
    row = db.get_fight(fight["id"])
    assert int(row["counters_a"] or 0) == 0
    assert int(row["counters_b"] or 0) == 0
    db.close()


def test_fight_submit_runs_balance_when_both_sides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proposed /fight with both sides: balance at submit, warning on card."""
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)

    from bot.commands import FightCog

    cog = object.__new__(FightCog)
    cog.bot = MagicMock()
    cog.bot.add_view = MagicMock()

    opponent = MagicMock()
    opponent.id = 20
    opponent.mention = "<@20>"

    interaction = MagicMock()
    interaction.user.id = 10
    interaction.guild_id = 1
    interaction.channel_id = 2
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    sent = MagicMock()
    sent.id = 999
    interaction.original_response = AsyncMock(return_value=sent)

    async def _run() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            with patch(
                "bot.commands.apply_balance_to_fight",
                side_effect=lambda db_, fid, **k: apply_balance_to_fight(
                    db_, fid, balance_fn=lambda *a, **kw: _lopsided()
                ),
            ):
                await FightCog.fight.callback(
                    cog,
                    interaction,
                    opponent=opponent,
                    champion_a="Aragorn",
                    champion_b="Goku",
                    context=None,
                    instant=False,
                    franchise=None,
                    exhibits=None,
                )

    asyncio.run(_run())
    interaction.response.send_message.assert_awaited()
    kwargs = interaction.response.send_message.await_args.kwargs
    embed = kwargs["embed"]
    names = [f.name for f in embed.fields]
    assert "⚖️ Referee's read" in names
    fights = db.list_fights(status="proposed")
    assert len(fights) == 1
    assert fights[0]["balance_warned"] is True
    labels = [getattr(i, "label", None) for i in kwargs["view"].children]
    assert "Counter (free)" in labels
    db.close()


def test_open_ended_fight_defers_balance_at_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)

    from bot.commands import FightCog

    cog = object.__new__(FightCog)
    cog.bot = MagicMock()
    cog.bot.add_view = MagicMock()

    opponent = MagicMock()
    opponent.id = 20
    opponent.mention = "<@20>"
    interaction = MagicMock()
    interaction.user.id = 10
    interaction.guild_id = 1
    interaction.channel_id = 2
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    sent = MagicMock()
    sent.id = 1001
    interaction.original_response = AsyncMock(return_value=sent)

    called = {"n": 0}

    def _bal(*a, **k):
        called["n"] += 1
        return _lopsided()

    async def _run() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN):
            with patch("bot.commands.apply_balance_to_fight", side_effect=_bal):
                await FightCog.fight.callback(
                    cog,
                    interaction,
                    opponent=opponent,
                    champion_a="Aragorn",
                    champion_b=None,
                    context=None,
                    instant=False,
                    franchise=None,
                    exhibits=None,
                )

    asyncio.run(_run())
    assert called["n"] == 0  # sides incomplete — not invoked
    row = db.list_fights(status="proposed")[0]
    assert row["open_ended"] is True
    assert row["side_b"] is None
    assert row["balance_warned"] is False
    embed = interaction.response.send_message.await_args.kwargs["embed"]
    assert "⚖️ Referee's read" not in [f.name for f in embed.fields]
    db.close()
