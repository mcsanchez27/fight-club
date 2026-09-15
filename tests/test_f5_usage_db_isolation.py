"""F5 — usage booking must land on the injected db, never a module fallback.

``budget.record_estimated_usage`` resolves ``db or get_db()`` against
*``bot.budget``'s* namespace. ``_persist_ruling`` patches and threads
``bot.commands.get_db``, so before this fix the usage call slipped past every
test's mock and wrote a row to the real ``data/court.db`` — one phantom
``usage_events`` row per full pytest run, booked against the monthly USD cap.

These tests fail loudly if the fallback ever returns: ``bot.budget.get_db`` is
patched to raise, so any unthreaded call blows up instead of quietly polluting.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bot.commands import _persist_ruling


def _verdict(**overrides: Any) -> dict[str, Any]:
    v: dict[str, Any] = {
        "matchup": "Goku vs Superman",
        "winner": "A",
        "confidence": 6,
        "ruling": "ruled",
        "steelman_a": "a",
        "steelman_b": "b",
        "citations": [],
        "concessions": [],
        "unknowns": [],
        "retrieval_status": "ok",
    }
    v.update(overrides)
    return v


def _persist(fake_db: MagicMock, **overrides: Any) -> int:
    """Run the persist path with the real DB fallback wired to explode."""
    kwargs: dict[str, Any] = {
        "message_id": 777,
        "channel_id": 202,
        "guild_id": 303,
        "fighter_a": "Goku",
        "fighter_b": "Superman",
        "context": None,
        "verdict": _verdict(),
    }
    kwargs.update(overrides)

    boom = AssertionError("budget.record_estimated_usage fell back to the real DB")
    with (
        patch("bot.commands.get_db", return_value=fake_db),
        patch("bot.budget.get_db", side_effect=boom),
    ):
        return _persist_ruling(**kwargs)


def test_usage_is_booked_on_the_injected_db() -> None:
    fake_db = MagicMock()
    fake_db.insert_ruling.return_value = 42

    ruling_id = _persist(fake_db)

    assert ruling_id == 42
    assert fake_db.record_usage.called, "usage never reached the injected db"
    booked = fake_db.record_usage.call_args.kwargs
    assert booked["tokens_in"] > 0
    assert booked["tokens_out"] > 0


def test_persist_ruling_never_touches_the_module_fallback() -> None:
    """The whole point: no call in this path may resolve its own get_db()."""
    fake_db = MagicMock()
    fake_db.insert_ruling.return_value = 1

    with patch("bot.budget.get_db") as budget_get_db:
        with patch("bot.commands.get_db", return_value=fake_db):
            _persist_ruling(
                message_id=778,
                channel_id=202,
                guild_id=303,
                fighter_a="Goku",
                fighter_b="Superman",
                context=None,
                verdict=_verdict(),
            )
        budget_get_db.assert_not_called()


def test_rejudge_enqueue_also_uses_the_injected_db() -> None:
    """``unavailable`` takes a second branch — it must use the same db."""
    fake_db = MagicMock()
    fake_db.insert_ruling.return_value = 9

    _persist(fake_db, verdict=_verdict(retrieval_status="unavailable"))

    assert fake_db.enqueue_rejudge.called
    assert fake_db.enqueue_rejudge.call_args.args[0] == 9


def test_real_provider_counts_win_over_estimates() -> None:
    """Anthropic's own token counts are booked verbatim when present."""
    fake_db = MagicMock()
    fake_db.insert_ruling.return_value = 5

    _persist(
        fake_db,
        verdict=_verdict(
            _usage={
                "input_tokens": 1234,
                "output_tokens": 567,
                "judge_seconds": 2.5,
                "retrieval_seconds": 1.25,
                "total_seconds": 4.0,
            }
        ),
    )

    booked = fake_db.record_usage.call_args.kwargs
    assert booked["tokens_in"] == 1234
    assert booked["tokens_out"] == 567
    assert booked["judge_seconds"] == 2.5
    assert booked["retrieval_seconds"] == 1.25
    assert booked["total_seconds"] == 4.0


@pytest.mark.parametrize(
    ("retrieval_status", "expected_in"),
    [("ok", 5000), ("unlisted", 2500)],
)
def test_estimates_used_when_provider_returned_no_usage(
    retrieval_status: str, expected_in: int
) -> None:
    """No ``_usage`` blob → design-doc estimates, higher when retrieval ran."""
    fake_db = MagicMock()
    fake_db.insert_ruling.return_value = 6

    _persist(fake_db, verdict=_verdict(retrieval_status=retrieval_status))

    booked = fake_db.record_usage.call_args.kwargs
    assert booked["tokens_in"] == expected_in
    assert booked["tokens_out"] == 1000
