"""CLI: python -m bot.cli "A vs B" — verdict without Discord."""

from __future__ import annotations

import argparse
import re
import sys

from dotenv import load_dotenv

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

    print(format_verdict_text(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
