"""CLI: python -m bot.cli "A vs B" — verdict without Discord."""

from __future__ import annotations

import argparse
import re
import sys

from dotenv import load_dotenv

from bot.budget import book_verdict_usage, current_month
from bot.db import get_db
from bot.judge import format_verdict_text, judge


def _split_matchup(text: str) -> tuple[str, str]:
    parts = re.split(r"\s+vs\.?\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        raise ValueError('Matchup must look like "Fighter A vs Fighter B"')
    return parts[0].strip(), parts[1].strip()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="python -m bot.cli",
        description="Fight Club Court — judge a matchup without Discord.",
    )
    parser.add_argument(
        "matchup",
        help='Matchup string, e.g. "Goku vs Superman"',
    )
    parser.add_argument(
        "-c",
        "--context",
        default=None,
        help="Optional conditions / arena / rules of engagement",
    )
    args = parser.parse_args(argv)

    try:
        a, b = _split_matchup(args.matchup)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    try:
        verdict = judge(a, b, context=args.context)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: Judge request failed: {e}", file=sys.stderr)
        return 1

    # A CLI fight is a real billed call, so it counts against the monthly cap
    # the same as a Discord ruling (NOTES 75). Never lose a verdict that has
    # already been paid for to a bookkeeping failure — warn and carry on.
    try:
        db = get_db()
        usd = book_verdict_usage(verdict, db=db)
        month = current_month()
        print(
            f"Recorded {usd:.4f} USD estimated "
            f"(month to date: {db.month_spend_usd(month):.4f}).",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"Warning: usage not recorded: {e}", file=sys.stderr)

    print(format_verdict_text(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
