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



THIN_RECORD_BANNER = "Thin record — ruled on available argument."


def _format_exhibit_ledger_field(ledger: Any) -> str:
    if ledger is None or ledger == "" or ledger == "(none)":
        return "—"
    if isinstance(ledger, list):
        lines: list[str] = []
        for item in ledger:
            if isinstance(item, dict):
                eid = item.get("exhibit_id") or item.get("id") or "?"
                status = item.get("status") or "unverified"
                note = item.get("weight_note") or ""
                bit = f"E{eid}: {status}"
                if note:
                    bit += f" — {note}"
                lines.append(bit)
            else:
                lines.append(str(item))
        return _join_list(lines)
    return str(ledger)


def ruling_drop_embed(
    v: dict[str, Any],
    *,
    fight: dict[str, Any] | None = None,
    thin_record: bool = False,
    exhibit_ledger: Any | None = None,
) -> discord.Embed:
    """Full thread ruling drop (item 8): steelmans → ledger → ruling → winner.

    Field lengths clipped like V1 ``verdict_embed``. Optional thin-record banner
    (Amendment 7) prepended to the description.
    """
    conf = float(v.get("confidence", 0) or 0)
    status = v.get("retrieval_status")
    unavailable = status in {"unavailable", "unlisted", "disabled"} or bool(
        v.get("voided")
    )
    if unavailable:
        color = discord.Color.dark_grey()
    elif conf >= 7:
        color = discord.Color.green()
    elif conf >= 4:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    matchup = v.get("matchup")
    if not matchup and fight:
        matchup = f"{fight.get('side_a') or '?'} vs {fight.get('side_b') or '?'}"
    matchup = matchup or "Matchup"

    ruling_text = str(v.get("ruling", "") or "")
    description = _clip(ruling_text, 3500)
    banner = None
    if thin_record or v.get("thin_record"):
        banner = str(v.get("thin_record_banner") or THIN_RECORD_BANNER)
        description = f"**{banner}**\n" + (description if description != "—" else "")
    if unavailable:
        description = (
            "**unverified: retrieval unavailable**\n"
            + (description if description != "—" else "")
        )

    embed = discord.Embed(
        title=f"⚖ {_clip(str(matchup), 250)}",
        description=_clip(description, 4000),
        color=color,
    )
    embed.add_field(
        name="Steelman A", value=_clip(str(v.get("steelman_a", ""))), inline=False
    )
    embed.add_field(
        name="Steelman B", value=_clip(str(v.get("steelman_b", ""))), inline=False
    )
    embed.add_field(
        name="Concessions",
        value=_clip(_join_list(list(v.get("concessions") or []))),
        inline=False,
    )

    ledger = exhibit_ledger
    if ledger is None:
        ledger = v.get("exhibit_ledger")
    embed.add_field(
        name="Exhibit ledger",
        value=_clip(_format_exhibit_ledger_field(ledger)),
        inline=False,
    )

    winner = None
    if fight and v.get("winner_side") in {"a", "b"}:
        winner = fight.get(f"side_{v['winner_side']}")
    if not winner:
        w = v.get("winner")
        # Ignore bare V1 side letters when fight sides are known.
        if w and not (fight and str(w).strip().lower() in {"a", "b"}):
            winner = w
    if not winner:
        winner = v.get("winner") or v.get("winner_side") or "?"
    embed.add_field(name="Winner", value=_clip(str(winner), 256), inline=True)
    embed.add_field(name="Confidence", value=f"{conf}/10", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    cites = v.get("citations") or []
    cite_lines = [_format_citation(c) for c in cites]
    embed.add_field(
        name="Citations",
        value=_clip(_join_list(cite_lines)),
        inline=False,
    )

    footer_bits = ["Fight Club Court · initial ruling"]
    if status:
        footer_bits.append(f"receipts: {status}")
    if banner:
        footer_bits.append("thin record")
    verified_n = sum(1 for c in cites if isinstance(c, dict) and c.get("verified"))
    unverified_n = len(cites) - verified_n
    footer_bits.append(f"✓{verified_n} ✗{unverified_n}")
    embed.set_footer(text=" · ".join(footer_bits))
    return embed
