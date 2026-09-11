"""Original context is carried through ChallengeModal re-judge."""

import asyncio
from unittest.mock import MagicMock, patch

from bot.commands import ChallengeModal, _last_verdict


def setup_function() -> None:
    _last_verdict.clear()


def test_challenge_modal_passes_stored_context() -> None:
    prior = {"matchup": "Goku vs Superman", "winner": "Superman"}
    modal = ChallengeModal(prior, context="no prep, random alley")

    captured: dict = {}

    def fake_judge(a, b, context=None, prior_verdict=None, challenge=None):
        captured["a"] = a
        captured["b"] = b
        captured["context"] = context
        captured["prior"] = prior_verdict
        captured["challenge"] = challenge
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

    interaction = MagicMock()
    interaction.response.defer = defer
    interaction.followup.send = followup_send

    # Modal TextInput: assign a simple string-like object
    modal.evidence = "new canon citation"  # type: ignore[assignment]

    async def run() -> None:
        with patch("bot.commands.judge", side_effect=fake_judge), patch(
            "bot.commands.verdict_embed", return_value=MagicMock()
        ), patch("bot.commands.ChallengeView", return_value=MagicMock()):
            await modal.on_submit(interaction)

    asyncio.run(run())

    assert captured["context"] == "no prep, random alley"
    assert captured["prior"] is prior
    assert "new canon" in str(captured["challenge"])


def test_store_ruling_keeps_context_for_later_challenge() -> None:
    from bot.commands import get_ruling, store_ruling

    store_ruling(
        42,
        {"matchup": "A vs B", "winner": "A"},
        context="bloodlusted, open field",
    )
    state = get_ruling(42)
    assert state is not None
    assert state["context"] == "bloodlusted, open field"
