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


def _resolve_matchup(v: dict[str, Any], fight: dict[str, Any] | None = None) -> str:
    matchup = str(v.get("matchup") or "").strip()
    if matchup and matchup not in {"—", "-", "Matchup"}:
        return matchup
    if fight:
        a = str(fight.get("side_a") or "").strip()
        b = str(fight.get("side_b") or "").strip()
        if a or b:
            return f"{a or '?'} vs {b or '?'}"
    return "Matchup"


def _resolve_winner_name(v: dict[str, Any], fight: dict[str, Any] | None = None) -> str:
    """Prefer character name over bare A/B (B2a)."""
    if fight and v.get("winner_side") in {"a", "b"}:
        named = fight.get(f"side_{v['winner_side']}")
        if named and str(named).strip():
            return str(named).strip()
    w = str(v.get("winner") or "").strip()
    if w and w.lower() not in {"a", "b"}:
        return w
    if v.get("winner_side") in {"a", "b"}:
        # Last resort: still better than "?" when sides unknown.
        return w.upper() if w.lower() in {"a", "b"} else str(v["winner_side"]).upper()
    return w or "?"


def _collapse_bullets(items: list[Any], *, limit: int = 3, max_chars: int = 280) -> str:
    """Trim steelman/concession/unknown lists so one embed fits without '…' spam."""
    cleaned = [str(i).strip() for i in (items or []) if str(i).strip()]
    if not cleaned:
        return "—"
    trimmed = cleaned[:limit]
    text = _join_list(trimmed)
    if len(cleaned) > limit:
        text += f"\n• (+{len(cleaned) - limit} more)"
    return _clip(text, max_chars)


def _ruling_description(v: dict[str, Any], *, unavailable: bool, thin: bool) -> str:
    """Compact body: optional banners + 3–5 sentence ruling (B1)."""
    ruling = str(v.get("ruling") or "").strip()
    # Prefer a short ruling; clip hard so steelmans don't blow the embed.
    body = _clip(ruling, 900)
    parts: list[str] = []
    if thin or v.get("thin_record"):
        banner = str(v.get("thin_record_banner") or THIN_RECORD_BANNER)
        parts.append(f"**{banner}**")
    if unavailable:
        parts.append("**unverified: retrieval unavailable**")
    if body and body != "—":
        parts.append(body)
    return _clip("\n".join(parts) if parts else "—", 1200)


def _receipt_footer_bits(v: dict[str, Any], *, status: Any, cites: list[Any]) -> list[str]:
    """Footer labels — never say 'voided' for a valid House Rule 3 ruling (B8)."""
    bits: list[str] = []
    if status:
        bits.append(f"receipts: {status}")
    # Retrieval gaps queue a re-judge; that is not a voided fight.
    if status == "unavailable" or (
        bool(v.get("voided")) and status in {"unavailable", "unlisted", "disabled"}
    ):
        bits.append("re-judge when receipts restore")
    elif v.get("voided"):
        # True lifecycle void (rare on an embed path).
        bits.append("voided")
    verified_n = sum(1 for c in cites if isinstance(c, dict) and c.get("verified"))
    unverified_n = len(cites) - verified_n
    bits.append(f"✓{verified_n} ✗{unverified_n}")
    return bits


def verdict_embed(v: dict[str, Any], *, fight: dict[str, Any] | None = None) -> discord.Embed:
    """Build a Discord embed from a judge verdict JSON dict (compact V2.1 shape)."""
    conf = float(v.get("confidence", 0) or 0)
    status = v.get("retrieval_status")
    unavailable = status in {"unavailable", "unlisted", "disabled"}

    if unavailable:
        color = discord.Color.dark_grey()
    elif conf >= 7:
        color = discord.Color.green()
    elif conf >= 4:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    matchup = _resolve_matchup(v, fight)
    winner = _resolve_winner_name(v, fight)

    embed = discord.Embed(
        title=f"⚔ {_clip(matchup, 250)}",
        description=_ruling_description(v, unavailable=unavailable, thin=False),
        color=color,
    )
    # Winner + confidence on one conceptual line (two inline fields).
    embed.add_field(
        name="Verdict",
        value=_clip(f"**{winner}** · {conf}/10", 256),
        inline=False,
    )
    # Collapsed steelmans / concessions / unknowns (B1).
    embed.add_field(
        name="Steelman A",
        value=_clip(str(v.get("steelman_a") or ""), 320),
        inline=False,
    )
    embed.add_field(
        name="Steelman B",
        value=_clip(str(v.get("steelman_b") or ""), 320),
        inline=False,
    )
    embed.add_field(
        name="Concessions",
        value=_collapse_bullets(list(v.get("concessions") or []), limit=3, max_chars=280),
        inline=False,
    )
    embed.add_field(
        name="Unknowns",
        value=_collapse_bullets(list(v.get("unknowns") or []), limit=3, max_chars=280),
        inline=False,
    )

    cites = v.get("citations") or []
    cite_lines = [_format_citation(c) for c in cites[:5]]
    embed.add_field(
        name="Citations",
        value=_clip(_join_list(cite_lines)),
        inline=False,
    )

    footer_bits = ["Fight Club Court · rulings revisable on new evidence"]
    footer_bits.extend(_receipt_footer_bits(v, status=status, cites=cites))
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
    """Challenge card for a ``proposed`` fight (Accept / Decline / Counter).

    Always echoes the parsed matchup (champion_a vs champion_b) before Accept.
    """
    side_a = str(fight.get("side_a") or "?")
    side_b = str(fight.get("side_b") or "?")
    open_ended = bool(fight.get("open_ended")) and not (
        fight.get("side_b") and str(fight.get("side_b")).strip()
    )
    if open_ended:
        title = f"⚔ Open challenge: {side_a} vs ?"
        description = (
            f"**Matchup:** {side_a} vs ?\n"
            "Open-ended — Accept and name your champion. Decline to void. Counter to rewrite."
        )
    else:
        title = f"⚔ Challenge: {side_a} vs {side_b}"
        description = (
            f"**Matchup:** {side_a} vs {side_b}\n"
            "Accept to open arguments. Decline to void. Counter to rewrite terms."
        )
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
    flare_line: str | None = None,
    title: str | None = None,
    diff_line: str | None = None,
) -> discord.Embed:
    """Full thread ruling drop — compact V2.1 shape (B1/B2/B2a)."""
    conf = float(v.get("confidence", 0) or 0)
    status = v.get("retrieval_status")
    unavailable = status in {"unavailable", "unlisted", "disabled"}
    if unavailable:
        color = discord.Color.dark_grey()
    elif conf >= 7:
        color = discord.Color.green()
    elif conf >= 4:
        color = discord.Color.gold()
    else:
        color = discord.Color.orange()

    matchup = _resolve_matchup(v, fight)
    winner = _resolve_winner_name(v, fight)

    embed_title = title if title else f"⚖ {_clip(matchup, 250)}"
    embed = discord.Embed(
        title=_clip(str(embed_title), 250),
        description=_ruling_description(
            v, unavailable=unavailable, thin=thin_record or bool(v.get("thin_record"))
        ),
        color=color,
    )
    diff = diff_line or v.get("reconsideration_diff")
    if diff:
        embed.add_field(name="Diff", value=_clip(str(diff), 256), inline=False)

    embed.add_field(
        name="Verdict",
        value=_clip(f"**{winner}** · {conf}/10", 256),
        inline=False,
    )
    embed.add_field(
        name="Steelman A", value=_clip(str(v.get("steelman_a", "")), 320), inline=False
    )
    embed.add_field(
        name="Steelman B", value=_clip(str(v.get("steelman_b", "")), 320), inline=False
    )
    embed.add_field(
        name="Concessions",
        value=_collapse_bullets(list(v.get("concessions") or []), limit=3, max_chars=280),
        inline=False,
    )

    ledger = exhibit_ledger
    if ledger is None:
        ledger = v.get("exhibit_ledger")
    embed.add_field(
        name="Exhibit ledger",
        value=_clip(_format_exhibit_ledger_field(ledger), 400),
        inline=False,
    )

    cites = v.get("citations") or []
    cite_lines = [_format_citation(c) for c in cites[:5]]
    embed.add_field(
        name="Citations",
        value=_clip(_join_list(cite_lines)),
        inline=False,
    )

    flare = flare_line or v.get("flare_line")
    if flare:
        embed.add_field(name="​", value=_clip(str(flare)), inline=False)

    if title and "reconsideration" in str(title).lower():
        footer_bits = ["Fight Club Court · reconsideration"]
    else:
        footer_bits = ["Fight Club Court · initial ruling"]
    footer_bits.extend(_receipt_footer_bits(v, status=status, cites=cites))
    if thin_record or v.get("thin_record"):
        footer_bits.append("thin record")
    embed.set_footer(text=" · ".join(footer_bits))
    return embed
