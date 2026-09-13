"""Discord embed formatter for Fight Club verdicts."""

from __future__ import annotations

from typing import Any

import discord


def _clip(text: str, limit: int = 1024) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text or "—"
    return text[: limit - 1] + "…"


def _join_list(items: list[str], empty: str = "—") -> str:
    if not items:
        return empty
    return "\n".join(f"• {i}" for i in items)


def _format_citation(c: Any) -> str:
    if isinstance(c, str):
        return f"✗ {c}"
    if not isinstance(c, dict):
        return f"✗ {c}"
    flag = "✓" if c.get("verified") else "✗"
    kind = c.get("kind") or "receipt"
    claim = c.get("claim") or ""
    loc = c.get("locator") or ""
    url = c.get("source_url") or ""
    parts = [f"{flag} [{kind}] {claim}".strip()]
    if loc:
        parts.append(loc)
    if url:
        parts.append(url)
    return " · ".join(p for p in parts if p)


def verdict_embed(v: dict[str, Any]) -> discord.Embed:
    """Build a Discord embed from a judge verdict JSON dict (steelman-first fields)."""
    conf = float(v.get("confidence", 0))
    status = v.get("retrieval_status")
    unavailable = status in {"unavailable", "unlisted", "disabled"} or bool(
        v.get("voided")
    )

    # color leans green high / amber mid / red low confidence
    if unavailable:
        color = discord.Color.dark_grey()
    elif conf >= 7:
        color = discord.Color.green()
    elif conf >= 4:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    description = _clip(str(v.get("ruling", "")), 3500)
    if unavailable:
        description = (
            "**unverified: retrieval unavailable**\n"
            + (description if description != "—" else "")
        )

    embed = discord.Embed(
        title=f"⚔ {_clip(str(v.get('matchup', 'Matchup')), 250)}",
        description=_clip(description, 4000),
        color=color,
    )
    embed.add_field(name="Steelman A", value=_clip(str(v.get("steelman_a", ""))), inline=False)
    embed.add_field(name="Steelman B", value=_clip(str(v.get("steelman_b", ""))), inline=False)
    embed.add_field(
        name="Concessions",
        value=_clip(_join_list(list(v.get("concessions") or []))),
        inline=False,
    )
    embed.add_field(
        name="Unknowns",
        value=_clip(_join_list(list(v.get("unknowns") or []))),
        inline=False,
    )
    embed.add_field(name="Winner", value=_clip(str(v.get("winner", "?")), 256), inline=True)
    embed.add_field(name="Confidence", value=f"{conf}/10", inline=True)
    embed.add_field(name="​", value="​", inline=True)

    cites = v.get("citations") or []
    cite_lines = [_format_citation(c) for c in cites]
    embed.add_field(
        name="Citations",
        value=_clip(_join_list(cite_lines)),
        inline=False,
    )

    footer_bits = ["Fight Club Court · rulings revisable on new evidence"]
    if status:
        footer_bits.append(f"receipts: {status}")
    if v.get("voided"):
        footer_bits.append("voided · queued for re-judge")
    verified_n = sum(
        1 for c in cites if isinstance(c, dict) and c.get("verified")
    )
    unverified_n = len(cites) - verified_n
    footer_bits.append(f"✓{verified_n} ✗{unverified_n}")
    embed.set_footer(text=" · ".join(footer_bits))
    return embed


def _format_balance_score(score: Any) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "?"
    if s == int(s):
        return str(int(s))
    return f"{s:.1f}"


def balance_warning_field(fight: dict[str, Any]) -> tuple[str, str] | None:
    """Return (name, value) for referee-read warning chrome, or None.

    Shown only when ``balance_warned`` and both sides are known (A1).
    Labeled as the referee's read — never a ruling.
    """
    if not fight or not fight.get("balance_warned"):
        return None
    from bot.fights import sides_complete

    if not sides_complete(fight):
        return None
    score_s = _format_balance_score(fight.get("balance_score"))
    favored = fight.get("balance_favored")
    reason = str(fight.get("balance_reason") or "").strip() or "lopsided matchup"
    if favored in {"a", "b"}:
        name = str(fight.get(f"side_{favored}") or "").strip() or f"side {favored}"
        value = f"{name} favored ({score_s}/10) — {reason}"
    else:
        value = f"even ({score_s}/10) — {reason}"
    return ("⚖️ Referee's read", value)


def challenge_card_embed(fight: dict[str, Any]) -> discord.Embed:
    """Challenge card for a ``proposed`` fight (Accept / Decline / Counter)."""
    side_a = str(fight.get("side_a") or "?")
    side_b = str(fight.get("side_b") or "?")
    open_ended = bool(fight.get("open_ended")) and not (
        fight.get("side_b") and str(fight.get("side_b")).strip()
    )
    if open_ended:
        title = f"⚔ Open challenge: {side_a} vs ?"
        description = (
            "Open-ended — Accept and name your champion. Decline to void. Counter to rewrite."
        )
    else:
        title = f"⚔ Challenge: {side_a} vs {side_b}"
        description = "Accept to open arguments. Decline to void. Counter to rewrite terms."
    embed = discord.Embed(
        title=_clip(title, 250),
        description=description,
        color=discord.Color.dark_gold(),
    )
    embed.add_field(name="Side A", value=_clip(side_a, 256), inline=True)
    embed.add_field(
        name="Side B",
        value=_clip(side_b if side_b != "?" else "(open — name on Accept)", 256),
        inline=True,
    )
    embed.add_field(name="​", value="​", inline=True)
    challenger = fight.get("challenger_id")
    challengee = fight.get("challengee_id")
    if challenger is not None:
        embed.add_field(name="Challenger", value=f"<@{int(challenger)}>", inline=True)
    if challengee is not None:
        embed.add_field(name="Challenged", value=f"<@{int(challengee)}>", inline=True)
    holder = challengee
    if holder is not None:
        embed.add_field(
            name="Buttons",
            value=f"<@{int(holder)}> holds Accept / Decline / Counter",
            inline=False,
        )
    ctx = fight.get("context")
    if ctx:
        embed.add_field(name="Context", value=_clip(str(ctx)), inline=False)
    ca = int(fight.get("counters_a") or 0)
    cb = int(fight.get("counters_b") or 0)
    if ca or cb:
        embed.add_field(
            name="Counters",
            value=f"A:{ca} · B:{cb}",
            inline=True,
        )
    # Balance warning chrome (referee's read; not a ruling).
    warn = balance_warning_field(fight)
    if warn is not None:
        embed.add_field(name=warn[0], value=_clip(warn[1]), inline=False)
    expires = fight.get("expires_at")
    if expires:
        embed.add_field(name="Expires", value=_clip(str(expires), 256), inline=False)
    fid = fight.get("id")
    footer = "Fight Club · challenge card"
    if fid is not None:
        footer += f" · fight #{int(fid)}"
    if open_ended:
        footer += " · open-ended"
    embed.set_footer(text=footer)
    return embed

