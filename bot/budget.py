"""Monthly USD cap + per-ruling token ceiling estimates."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from bot.db import CourtDB, get_db


def monthly_usd_cap(
    db: CourtDB | None = None, guild_id: int | None = None
) -> float:
    from bot.config import get_guild_config

    return float(get_guild_config(db, guild_id, "monthly_usd_cap"))


def token_ceiling() -> int:
    """Max estimated tokens (in+out) per ruling before hard stop."""
    return int(os.getenv("FIGHT_TOKEN_CEILING", "16000"))


def input_usd_per_mtok() -> float:
    return float(os.getenv("FIGHT_USD_PER_MTOK_INPUT", "2.0"))


def output_usd_per_mtok() -> float:
    return float(os.getenv("FIGHT_USD_PER_MTOK_OUTPUT", "10.0"))


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token)."""
    return max(1, len(text or "") // 4)


def estimate_usd(tokens_in: int, tokens_out: int) -> float:
    return (tokens_in / 1_000_000.0) * input_usd_per_mtok() + (
        tokens_out / 1_000_000.0
    ) * output_usd_per_mtok()


def estimate_ruling_cost_usd(*, with_retrieval: bool = True) -> float:
    """Design-doc style ballpark used for pre-flight checks.

    Phase-2 baseline ~2.5k in + 1k out; retrieval ~doubles input.
    """
    tin = 5000 if with_retrieval else 2500
    tout = 1000
    return estimate_usd(tin, tout)


def check_budget(
    db: CourtDB | None = None, guild_id: int | None = None
) -> str | None:
    """Return ephemeral rejection if monthly cap or token ceiling would be breached."""
    db = db or get_db()
    cap = monthly_usd_cap(db, guild_id)
    if cap > 0:
        try:
            spent = float(db.month_spend_usd(current_month()) or 0.0)
        except (TypeError, ValueError):
            spent = 0.0
        est = estimate_ruling_cost_usd(with_retrieval=True)
        if spent + est > cap:
            return (
                f"Monthly Fight Club spend cap reached "
                f"(${spent:.2f} of ${cap:.2f}). Court recesses until next month."
            )
    ceiling = token_ceiling()
    # Pre-flight uses the design estimate; hard ceiling blocks oversized packs later too
    est_tokens = 5000 + 1000
    if ceiling > 0 and est_tokens > ceiling:
        return (
            f"Per-ruling token ceiling ({ceiling}) is below the estimated "
            f"judge pack ({est_tokens}). Raise FIGHT_TOKEN_CEILING or disable retrieval."
        )
    return None


def assert_token_pack(prompt_tokens: int, max_out: int = 2048) -> str | None:
    ceiling = token_ceiling()
    total = prompt_tokens + max_out
    if ceiling > 0 and total > ceiling:
        return (
            f"This ruling's estimated tokens ({total}) exceed "
            f"FIGHT_TOKEN_CEILING ({ceiling})."
        )
    return None


def record_estimated_usage(
    *,
    tokens_in: int,
    tokens_out: int,
    db: CourtDB | None = None,
    retrieval_seconds: float = 0.0,
    judge_seconds: float = 0.0,
    total_seconds: float = 0.0,
) -> float:
    """Book usage. Prefer real Anthropic token counts when available."""
    db = db or get_db()
    usd = estimate_usd(tokens_in, tokens_out)
    db.record_usage(
        month=current_month(),
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        estimated_usd=usd,
        retrieval_seconds=retrieval_seconds,
        judge_seconds=judge_seconds,
        total_seconds=total_seconds,
    )
    return usd


def usage_from_verdict(verdict: dict) -> dict:
    """Extract token + stage timing fields from a judge verdict's ``_usage`` blob."""
    raw = (verdict or {}).get("_usage") or {}
    return {
        "tokens_in": int(raw.get("input_tokens") or 0),
        "tokens_out": int(raw.get("output_tokens") or 0),
        "retrieval_seconds": float(raw.get("retrieval_seconds") or 0.0),
        "judge_seconds": float(raw.get("judge_seconds") or 0.0),
        "total_seconds": float(raw.get("total_seconds") or 0.0),
    }


def book_verdict_usage(verdict: dict, *, db: CourtDB | None = None) -> float:
    """Book one verdict's usage, preferring real provider counts over estimates.

    Single source of the estimate fallback: the Discord path (_persist_ruling)
    and the CLI both route through here so the two cannot drift apart. Returns
    the estimated USD booked.
    """
    u = usage_from_verdict(verdict)
    tokens_in = u["tokens_in"]
    tokens_out = u["tokens_out"]
    # Fallback to design-doc estimates when the provider did not return usage.
    if tokens_in <= 0 and tokens_out <= 0:
        tokens_in = 5000 if (verdict or {}).get("retrieval_status") == "ok" else 2500
        tokens_out = 1000
    return record_estimated_usage(
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        db=db,
        retrieval_seconds=u["retrieval_seconds"],
        judge_seconds=u["judge_seconds"],
        total_seconds=u["total_seconds"],
    )

