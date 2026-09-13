"""Slash commands and Challenge button view."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.budget import (
    assert_token_pack,
    check_budget,
    estimate_tokens,
    record_estimated_usage,
    usage_from_verdict,
)
from bot.db import get_db
from bot.embeds import verdict_embed
from bot.export import export_markdown
from bot.judge import judge
from bot.limits import limiter
from bot.progress import (
    PROGRESS_JUDGING,
    PROGRESS_RETRIEVING,
    edit_deferred_progress,
)
from bot.retrieval import retrieve

log = logging.getLogger("fightclub")

_last_verdict: dict[int, dict[str, Any]] = {}


def store_ruling(
    message_id: int,
    verdict: dict[str, Any],
    *,
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
    ruling_id: int | None = None,
) -> None:
    _last_verdict[message_id] = {
        "verdict": verdict,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "context": context,
        "ruling_id": ruling_id,
    }


def get_ruling(message_id: int) -> dict[str, Any] | None:
    state = _last_verdict.get(message_id)
    if state:
        return state
    row = get_db().get_ruling_by_message_id(message_id)
    if not row:
        return None
    state = {
        "verdict": row["verdict"],
        "fighter_a": row["fighter_a"],
        "fighter_b": row["fighter_b"],
        "context": row["context"],
        "ruling_id": row["id"],
    }
    _last_verdict[message_id] = state
    return state


def _persist_citations(ruling_id: int, verdict: dict[str, Any]) -> None:
    db = get_db()
    for c in verdict.get("citations") or []:
        if isinstance(c, str):
            db.insert_citation(
                ruling_id=ruling_id, claim=c, source_url=None, locator=None,
                snippet=None, verified=False, retrieved_at=None, kind="receipt",
            )
            continue
        if not isinstance(c, dict):
            continue
        db.insert_citation(
            ruling_id=ruling_id,
            claim=str(c.get("claim") or ""),
            source_url=str(c.get("source_url") or "") or None,
            locator=str(c.get("locator") or "") or None,
            snippet=str(c.get("snippet") or "") or None,
            verified=bool(c.get("verified")),
            retrieved_at=str(c.get("retrieved_at") or "") or None,
            kind=str(c.get("kind") or "receipt"),
        )


def _persist_ruling(
    *,
    message_id: int,
    channel_id: int | None,
    guild_id: int | None,
    fighter_a: str,
    fighter_b: str,
    context: str | None,
    verdict: dict[str, Any],
    parent_ruling_id: int | None = None,
) -> int:
    voided = bool(verdict.get("voided"))
    retrieval_status = verdict.get("retrieval_status")
    franchise = verdict.get("franchise")
    ruling_id = get_db().insert_ruling(
        message_id=message_id,
        channel_id=channel_id,
        guild_id=guild_id,
        fighter_a=fighter_a,
        fighter_b=fighter_b,
        context=context,
        verdict=verdict,
        parent_ruling_id=parent_ruling_id,
        franchise=franchise if isinstance(franchise, str) else None,
        retrieval_status=retrieval_status if isinstance(retrieval_status, str) else None,
        voided=voided,
    )
    _persist_citations(ruling_id, verdict)
    # Only auto-retry hard fetch failures. Unlisted/disabled never become
    # ok without a config change, so don't spin the queue forever.
    if retrieval_status == "unavailable":
        get_db().enqueue_rejudge(
            ruling_id,
            reason=f"retrieval_status={retrieval_status}",
        )
    store_ruling(
        message_id, verdict, fighter_a=fighter_a, fighter_b=fighter_b,
        context=context, ruling_id=ruling_id,
    )
    u = usage_from_verdict(verdict)
    tin = u["tokens_in"]
    tout = u["tokens_out"]
    # Fallback to design-doc estimates when the provider did not return usage.
    if tin <= 0 and tout <= 0:
        tin = 5000 if retrieval_status == "ok" else 2500
        tout = 1000
    record_estimated_usage(
        tokens_in=tin,
        tokens_out=tout,
        retrieval_seconds=u["retrieval_seconds"],
        judge_seconds=u["judge_seconds"],
        total_seconds=u["total_seconds"],
    )
    return ruling_id


def _preflight(interaction: discord.Interaction) -> str | None:
    reject = limiter.check(interaction.user.id, interaction.guild_id)
    if reject:
        return reject
    return check_budget()


class ChallengeModal(discord.ui.Modal, title="Challenge the ruling"):
    evidence = discord.ui.TextInput(
        label="New evidence / challenge",
        style=discord.TextStyle.paragraph,
        placeholder="Cite canon, logistics, or a win-condition the court missed…",
        required=True,
        max_length=1500,
    )

    def __init__(self, prior: dict[str, Any], *, fighter_a: str, fighter_b: str, context: str | None, parent_ruling_id: int | None):
        super().__init__()
        self.prior = prior
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b
        self.context = context
        self.parent_ruling_id = parent_ruling_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reject = _preflight(interaction)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await edit_deferred_progress(interaction, PROGRESS_RETRIEVING)
        try:
            result = await asyncio.to_thread(
                retrieve,
                self.fighter_a,
                self.fighter_b,
                self.context,
                exhibits=[str(self.evidence)],
            )
            await edit_deferred_progress(interaction, PROGRESS_JUDGING)
            verdict = await asyncio.to_thread(
                judge,
                self.fighter_a,
                self.fighter_b,
                self.context,
                self.prior,
                str(self.evidence),
                exhibits=[str(self.evidence)],
                retrieval_result=result,
            )
        except Exception as e:
            await interaction.followup.send(f"Re-judge failed: {e}", ephemeral=True)
            return
        limiter.record(interaction.user.id, interaction.guild_id)
        msg = await interaction.followup.send(
            content="⚖ Court revises on challenge:",
            embed=verdict_embed(verdict),
            view=ChallengeView(),
        )
        _persist_ruling(
            message_id=msg.id, channel_id=interaction.channel_id, guild_id=interaction.guild_id,
            fighter_a=self.fighter_a, fighter_b=self.fighter_b, context=self.context,
            verdict=verdict, parent_ruling_id=self.parent_ruling_id,
        )


class ChallengeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Challenge", style=discord.ButtonStyle.danger, custom_id="fightclub:challenge")
    async def challenge(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        message = interaction.message
        state = get_ruling(message.id) if message is not None else None
        if not state:
            await interaction.response.send_message(
                "No ruling attached to this message to challenge. Run `/fight` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            ChallengeModal(
                state["verdict"], fighter_a=state["fighter_a"], fighter_b=state["fighter_b"],
                context=state.get("context"), parent_ruling_id=state.get("ruling_id"),
            )
        )


class FightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rejudge_loop.start()

    def cog_unload(self) -> None:
        self.rejudge_loop.cancel()

    @tasks.loop(minutes=15)
    async def rejudge_loop(self) -> None:
        await self._process_rejudge_queue()

    @rejudge_loop.before_loop
    async def before_rejudge_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _process_rejudge_queue(self) -> None:
        db = get_db()
        pending = db.list_pending_rejudges(limit=5)
        for item in pending:
            if check_budget(db):
                break
            fa, fb = item["fighter_a"], item["fighter_b"]
            ctx = item.get("context")
            franchise = item.get("franchise")
            try:
                result = await asyncio.to_thread(retrieve, fa, fb, ctx, franchise_hint=franchise)
            except Exception as e:
                log.warning("rejudge retrieve failed for %s: %s", item["ruling_id"], e)
                continue
            if result.retrieval_unavailable:
                continue
            try:
                verdict = await asyncio.to_thread(
                    judge, fa, fb, ctx, item.get("verdict"),
                    "Automatic re-judge: retrieval restored.",
                    franchise=franchise, retrieval_result=result,
                )
            except Exception as e:
                log.warning("rejudge judge failed for %s: %s", item["ruling_id"], e)
                db.mark_rejudge_done(item["ruling_id"], status="failed")
                continue
            channel_id = item.get("channel_id")
            channel = self.bot.get_channel(channel_id) if channel_id else None
            message_id = None
            if channel is not None and hasattr(channel, "send"):
                try:
                    msg = await channel.send(
                        content=f"⚖ Automatic re-judge for voided ruling #{item['ruling_id']} (retrieval restored):",
                        embed=verdict_embed(verdict), view=ChallengeView(),
                    )
                    message_id = msg.id
                except Exception as e:
                    log.warning("rejudge send failed: %s", e)
            new_id = _persist_ruling(
                message_id=message_id or 0, channel_id=channel_id, guild_id=item.get("guild_id"),
                fighter_a=fa, fighter_b=fb, context=ctx, verdict=verdict,
                parent_ruling_id=item["ruling_id"],
            )
            db.mark_rejudge_done(item["ruling_id"], status="done")
            log.info("rejudge complete parent=%s new=%s status=%s", item["ruling_id"], new_id, verdict.get("retrieval_status"))

    @app_commands.command(name="fight", description="Judge a fiction / death-battle matchup")
    @app_commands.describe(
        fighter_a="First fighter / faction",
        fighter_b="Second fighter / faction",
        context="Optional arena, rules, or constraints",
        franchise="Optional franchise key/label (dragon_ball, asoiaf, lotr, vikings)",
        exhibits="Optional user-pasted evidence (EXHIBIT; court tries to verify)",
    )
    async def fight(
        self, interaction: discord.Interaction, fighter_a: str, fighter_b: str,
        context: str | None = None, franchise: str | None = None, exhibits: str | None = None,
    ) -> None:
        reject = _preflight(interaction)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        exhibit_list = [exhibits] if exhibits and exhibits.strip() else []
        await edit_deferred_progress(interaction, PROGRESS_RETRIEVING)
        try:
            result = await asyncio.to_thread(
                retrieve, fighter_a, fighter_b, context,
                franchise_hint=franchise, exhibits=exhibit_list,
            )
            from bot.retrieval import pack_retrieval_for_prompt
            approx_prompt = pack_retrieval_for_prompt(result) + ("x" * 4000)
            tok_reject = assert_token_pack(estimate_tokens(approx_prompt), max_out=2048)
            if tok_reject:
                await interaction.followup.send(tok_reject, ephemeral=True)
                return
            await edit_deferred_progress(interaction, PROGRESS_JUDGING)
            verdict = await asyncio.to_thread(
                judge, fighter_a, fighter_b, context, None, None,
                exhibits=exhibit_list, franchise=franchise, retrieval_result=result,
            )
        except Exception as e:
            await interaction.followup.send(f"Judgment failed: {e}", ephemeral=True)
            return
        limiter.record(interaction.user.id, interaction.guild_id)
        msg = await interaction.followup.send(embed=verdict_embed(verdict), view=ChallengeView())
        _persist_ruling(
            message_id=msg.id, channel_id=interaction.channel_id, guild_id=interaction.guild_id,
            fighter_a=fighter_a, fighter_b=fighter_b, context=context, verdict=verdict,
            parent_ruling_id=None,
        )

    @app_commands.command(name="export", description="Export a ruling as a markdown block for paste-anywhere")
    @app_commands.describe(message_id="Discord message ID of the ruling (default: reply/reference or latest you can see)")
    async def export_cmd(self, interaction: discord.Interaction, message_id: str | None = None) -> None:
        mid: int | None = None
        if message_id:
            try:
                mid = int(message_id.strip())
            except ValueError:
                await interaction.response.send_message("message_id must be an integer snowflake.", ephemeral=True)
                return
        elif interaction.message and interaction.message.reference:
            mid = interaction.message.reference.message_id
        row = None
        state = None
        if mid is not None:
            state = get_ruling(mid)
            row = get_db().get_ruling_by_message_id(mid)
        elif interaction.guild_id is not None:
            rows = get_db().list_guild_rulings(interaction.guild_id, limit=1)
            row = rows[0] if rows else None
        if not row and not state:
            await interaction.response.send_message("No ruling found. Pass message_id or run `/fight` first.", ephemeral=True)
            return
        if row:
            verdict = row["verdict"]
            fa, fb, ctx = row["fighter_a"], row["fighter_b"], row.get("context")
            franchise = row.get("franchise")
            status = row.get("retrieval_status")
            voided = bool(row.get("voided"))
            cites = get_db().list_citations(row["id"]) or verdict.get("citations")
        else:
            assert state is not None
            verdict = state["verdict"]
            fa, fb, ctx = state["fighter_a"], state["fighter_b"], state.get("context")
            franchise = verdict.get("franchise")
            status = verdict.get("retrieval_status")
            voided = bool(verdict.get("voided"))
            cites = verdict.get("citations")
        md = export_markdown(verdict, fighter_a=fa, fighter_b=fb, context=ctx, franchise=franchise, retrieval_status=status, voided=voided, citations=cites)
        if len(md) > 1900:
            md = md[:1890] + "\n```"
        await interaction.response.send_message(md, ephemeral=True)

    @app_commands.command(name="standings", description="Recent Fight Club rulings in this server")
    @app_commands.describe(limit="How many recent rulings to show (1-25, default 10)")
    async def standings(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 10) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Standings are only available in a server.", ephemeral=True)
            return
        rows = get_db().list_guild_rulings(interaction.guild_id, limit=int(limit))
        if not rows:
            await interaction.response.send_message("No rulings on the record yet. Run `/fight`.", ephemeral=True)
            return
        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            v = row["verdict"]
            matchup = v.get("matchup") or f"{row['fighter_a']} vs {row['fighter_b']}"
            winner = v.get("winner", "?")
            conf = v.get("confidence", "?")
            revised = "revised" if row.get("parent_ruling_id") else "original"
            void = " · voided" if row.get("voided") else ""
            lines.append(f"{i}. **{matchup}** — {winner} ({conf}/10) · {revised}{void}")
        embed = discord.Embed(title="⚔ Court standings", description="\n".join(lines), color=discord.Color.dark_gold())
        embed.set_footer(text=f"Last {len(rows)} ruling(s) in this server")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="laws", description="List the Laws of the Court")
    async def laws(self, interaction: discord.Interaction) -> None:
        from bot.laws import get_laws
        text = get_laws().strip()
        if not text:
            await interaction.response.send_message("No laws loaded. Check laws.md on the bot host.", ephemeral=True)
            return
        body = text if len(text) <= 4000 else text[:3999] + "…"
        embed = discord.Embed(title="⚖ Laws of the Court", description=body, color=discord.Color.dark_teal())
        await interaction.response.send_message(embed=embed)

    docket = app_commands.Group(name="docket", description="Banked matchups for this server")

    @docket.command(name="add", description="Bank a matchup for later")
    @app_commands.describe(matchup='Matchup label, e.g. "Goku vs Superman"', notes="Optional notes / constraints")
    async def docket_add(self, interaction: discord.Interaction, matchup: str, notes: str | None = None) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Docket is only available in a server.", ephemeral=True)
            return
        rid = get_db().add_docket(interaction.guild_id, matchup.strip(), notes)
        await interaction.response.send_message(
            f"Docketed #{rid}: **{matchup.strip()}**" + (f" — _{notes}_" if notes else ""),
            ephemeral=True,
        )

    @docket.command(name="list", description="List banked matchups for this server")
    @app_commands.describe(limit="How many entries to show (1-25, default 15)")
    async def docket_list(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 25] = 15) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Docket is only available in a server.", ephemeral=True)
            return
        rows = get_db().list_docket(interaction.guild_id, limit=int(limit))
        if not rows:
            await interaction.response.send_message("Docket is empty. Use `/docket add`.", ephemeral=True)
            return
        lines = []
        for row in rows:
            note = f" — _{row['notes']}_" if row.get("notes") else ""
            lines.append(f"#{row['id']}: **{row['matchup']}**{note}")
        embed = discord.Embed(title="📋 Banked docket", description="\n".join(lines), color=discord.Color.dark_blue())
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    from bot.laws import load_laws
    get_db()
    load_laws()
    await bot.add_cog(FightCog(bot))
    bot.add_view(ChallengeView())
