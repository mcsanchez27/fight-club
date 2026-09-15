"""F6 — the CLI books usage too, and both paths agree on the numbers.

NOTES 75: ``bot/cli.py`` called ``judge()`` and printed, never touching the db,
so a real billed CLI fight was invisible to ``month_spend_usd`` and the monthly
USD cap. Discord rulings were booked; CLI fights were not.

``budget.book_verdict_usage`` is now the single source of the estimate fallback
and both paths route through it, so the two cannot drift.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bot.budget import book_verdict_usage
from bot.cli import main


def _verdict(**overrides: Any) -> dict[str, Any]:
    v: dict[str, Any] = {
        "matchup": "Goku vs Superman",
        "winner": "A",
        "winner_side": "A",
        "confidence": 6,
        "ruling": "ruled on the record",
        "steelman_a": "a",
        "steelman_b": "b",
        "citations": [],
        "concessions": [],
        "unknowns": [],
        "retrieval_status": "ok",
    }
    v.update(overrides)
    return v


def _run_cli(fake_db: MagicMock, verdict: dict[str, Any], argv: list[str]) -> int:
    with (
        patch("bot.cli.load_dotenv"),
        patch("bot.cli.judge", return_value=verdict),
        patch("bot.cli.get_db", return_value=fake_db),
    ):
        return main(argv)


def test_cli_books_usage_on_the_db() -> None:
    fake_db = MagicMock()
    fake_db.month_spend_usd.return_value = 0.015

    rc = _run_cli(fake_db, _verdict(), ["Goku vs Superman"])

    assert rc == 0
    assert fake_db.record_usage.called, "CLI fight was not booked (NOTES 75)"
    booked = fake_db.record_usage.call_args.kwargs
    assert booked["tokens_in"] > 0
    assert booked["tokens_out"] > 0
    assert booked["estimated_usd"] > 0


def test_cli_prefers_real_provider_counts() -> None:
    fake_db = MagicMock()
    fake_db.month_spend_usd.return_value = 0.0
    verdict = _verdict(
        _usage={"input_tokens": 1234, "output_tokens": 567, "judge_seconds": 2.0}
    )

    _run_cli(fake_db, verdict, ["Goku vs Superman"])

    booked = fake_db.record_usage.call_args.kwargs
    assert booked["tokens_in"] == 1234
    assert booked["tokens_out"] == 567
    assert booked["judge_seconds"] == 2.0


def test_cli_still_prints_the_verdict_when_booking_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A paid-for verdict must never be lost to a bookkeeping failure."""
    with (
        patch("bot.cli.load_dotenv"),
        patch("bot.cli.judge", return_value=_verdict()),
        patch("bot.cli.get_db", side_effect=RuntimeError("db unavailable")),
    ):
        rc = main(["Goku vs Superman"])

    out = capsys.readouterr()
    assert rc == 0
    assert "ruled on the record" in out.out
    assert "usage not recorded" in out.err


def test_cli_reports_month_to_date_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spend goes to stderr so piping the verdict stays clean."""
    fake_db = MagicMock()
    fake_db.month_spend_usd.return_value = 1.25

    _run_cli(fake_db, _verdict(), ["Goku vs Superman"])

    out = capsys.readouterr()
    assert "month to date" in out.err
    assert "1.25" in out.err
    assert "month to date" not in out.out


def test_bad_matchup_never_books_anything() -> None:
    """Argument errors exit before the judge — nothing to bill, nothing to book."""
    fake_db = MagicMock()
    with (
        patch("bot.cli.load_dotenv"),
        patch("bot.cli.judge") as judge_fn,
        patch("bot.cli.get_db", return_value=fake_db),
    ):
        rc = main(["not a matchup"])

    assert rc == 2
    judge_fn.assert_not_called()
    fake_db.record_usage.assert_not_called()


@pytest.mark.parametrize(
    ("retrieval_status", "expected_in"),
    [("ok", 5000), ("unlisted", 2500), ("unavailable", 2500)],
)
def test_shared_fallback_matches_design_doc(
    retrieval_status: str, expected_in: int
) -> None:
    """Both paths share this helper, so the estimate rule lives in one place."""
    fake_db = MagicMock()
    book_verdict_usage(_verdict(retrieval_status=retrieval_status), db=fake_db)

    booked = fake_db.record_usage.call_args.kwargs
    assert booked["tokens_in"] == expected_in
    assert booked["tokens_out"] == 1000


def test_cli_and_discord_paths_book_identically() -> None:
    """Parity: the same verdict books the same numbers through either path."""
    verdict = _verdict()

    cli_db = MagicMock()
    cli_db.month_spend_usd.return_value = 0.0
    _run_cli(cli_db, verdict, ["Goku vs Superman"])
    cli_booked = cli_db.record_usage.call_args.kwargs

    discord_db = MagicMock()
    book_verdict_usage(verdict, db=discord_db)
    discord_booked = discord_db.record_usage.call_args.kwargs

    for field in ("tokens_in", "tokens_out", "estimated_usd"):
        assert cli_booked[field] == discord_booked[field], f"{field} drifted"
