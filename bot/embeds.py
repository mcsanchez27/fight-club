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
