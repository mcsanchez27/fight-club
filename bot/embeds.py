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


def verdict_embed(v: dict[str, Any]) -> discord.Embed:
    """Build a Discord embed from a judge verdict JSON dict (steelman-first fields)."""
    conf = float(v.get("confidence", 0))
    # color leans green high / amber mid / red low confidence
    if conf >= 7:
        color = discord.Color.green()
    elif conf >= 4:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    embed = discord.Embed(
        title=f"⚔ {_clip(str(v.get('matchup', 'Matchup')), 250)}",
        description=_clip(str(v.get("ruling", "")), 4000),
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
    embed.add_field(
        name="Citations",
        value=_clip(_join_list(list(v.get("citations") or []))),
        inline=False,
    )
    embed.set_footer(text="Fight Club Court · rulings revisable on new evidence")
    return embed
