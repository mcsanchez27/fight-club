"""Original context and fighters are carried through ChallengeModal re-judge."""

import asyncio
from unittest.mock import MagicMock, patch

from bot.commands import ChallengeModal, _last_verdict, get_ruling, store_ruling


def setup_function() -> None:
    _last_verdict.clear()


def test_challenge_modal_passes_fighters_and_context_explicitly() -> None:
    prior = {"matchup": "Goku vs Superman", "winner": "Superman"}
    modal = ChallengeModal(
        prior,
        fighter_a="Goku",
        fighter_b="Superman",
        context="no prep, random alley",
        parent_ruling_id=1,
    )

    captured: dict = {}

    def fake_judge(a, b, context=None, prior_verdict=None, challenge=None, **kwargs):
        captured["a"] = a
        captured["b"] = b
        captured["context"] = context
        captured["prior"] = prior_verdict
        captured["challenge"] = challenge
        captured["kwargs"] = kwargs
        return {
            "matchup": "Goku vs Superman",
            "winner": "Goku",
            "confidence": 6,
            "ruling": "revised",
            "steelman_a": "a",
            "steelman_b": "b",
            "citations": [],
            "concessions": [],
            "unknowns": [],
        }

    async def defer(**kwargs):
        return None

    async def followup_send(*args, **kwargs):
        msg = MagicMock()
        msg.id = 777
        return msg

    async def edit_original_response(**kwargs):
        return None

    interaction = MagicMock()
    interaction.response.defer = defer
    interaction.followup.send = followup_send
    interaction.edit_original_response = edit_original_response
    modal.evidence = "new canon citation"  # type: ignore[assignment]

    async def run() -> None:
        fake_db = MagicMock()
        fake_db.insert_ruling.return_value = 2
        fake_retrieval = MagicMock()
        fake_retrieval.retrieval_seconds = 0.1
        with patch("bot.commands.judge", side_effect=fake_judge), patch(
            "bot.commands.retrieve", return_value=fake_retrieval
        ), patch(
            "bot.commands.verdict_embed", return_value=MagicMock()
        ), patch("bot.commands.ChallengeView", return_value=MagicMock()), patch(
            "bot.commands.get_db", return_value=fake_db
        ), patch("bot.commands.limiter") as lim:
            lim.check.return_value = None
            await modal.on_submit(interaction)

    asyncio.run(run())

    assert captured["a"] == "Goku"
    assert captured["b"] == "Superman"
    assert captured["context"] == "no prep, random alley"
    assert captured["prior"] is prior
    assert "new canon" in str(captured["challenge"])
    # Names with " vs " in them must not be re-parsed from matchup
    assert " vs " not in captured["a"]


def test_store_ruling_keeps_context_for_later_challenge() -> None:
    store_ruling(
        42,
        {"matchup": "A vs B", "winner": "A"},
        fighter_a="A",
        fighter_b="B",
        context="bloodlusted, open field",
    )
    state = get_ruling(42)
    assert state is not None
    assert state["context"] == "bloodlusted, open field"
    assert state["fighter_a"] == "A"
    assert state["fighter_b"] == "B"


def test_fighters_with_vs_in_name_are_not_split() -> None:
    """Fighter names containing ' vs ' must be passed through untouched."""
    store_ruling(
        99,
        {"matchup": "X vs Y", "winner": "X"},
        fighter_a="Oddjob vs Spectre film",
        fighter_b="Bond",
        context=None,
    )
    state = get_ruling(99)
    assert state["fighter_a"] == "Oddjob vs Spectre film"
    assert state["fighter_b"] == "Bond"
