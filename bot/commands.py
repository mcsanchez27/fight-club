"""Slash commands and Challenge button view."""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.db import get_db
from bot.embeds import verdict_embed
from bot.judge import judge
from bot.limits import limiter

# ruling state per ruling message id (in-memory only)
# value: {verdict, fighter_a, fighter_b, context, ruling_id}
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
    """Associate a verdict and matchup fields with the Discord message that displayed it."""
    _last_verdict[message_id] = {
        "verdict": verdict,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "context": context,
        "ruling_id": ruling_id,
    }


def get_ruling(message_id: int) -> dict[str, Any] | None:
    """Look up stored ruling state for a message (memory, then SQLite)."""
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
    ruling_id = get_db().insert_ruling(
        message_id=message_id,
        channel_id=channel_id,
        guild_id=guild_id,
        fighter_a=fighter_a,
        fighter_b=fighter_b,
        context=context,
        verdict=verdict,
        parent_ruling_id=parent_ruling_id,
    )
    store_ruling(
        message_id,
        verdict,
        fighter_a=fighter_a,
        fighter_b=fighter_b,
        context=context,
        ruling_id=ruling_id,
    )
    return ruling_id


class ChallengeModal(discord.ui.Modal, title="Challenge the ruling"):
    evidence = discord.ui.TextInput(
        label="New evidence / challenge",
        style=discord.TextStyle.paragraph,
        placeholder="Cite canon, logistics, or a win-condition the court missed…",
        required=True,
        max_length=1500,
    )

    def __init__(
        self,
        prior: dict[str, Any],
        *,
        fighter_a: str,
        fighter_b: str,
        context: str | None,
        parent_ruling_id: int | None,
    ):
        super().__init__()
        self.prior = prior
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b
        self.context = context
        self.parent_ruling_id = parent_ruling_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        reject = limiter.check(interaction.user.id, interaction.guild_id)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        limiter.record(interaction.user.id, interaction.guild_id)
        try:
            verdict = await asyncio.to_thread(
                judge,
                self.fighter_a,
                self.fighter_b,
                self.context,
                self.prior,
                str(self.evidence),
            )
        except Exception as e:
            await interaction.followup.send(f"Re-judge failed: {e}", ephemeral=True)
            return
        msg = await interaction.followup.send(
            content="⚖ Court revises on challenge:",
            embed=verdict_embed(verdict),
            view=ChallengeView(),
        )
        _persist_ruling(
            message_id=msg.id,
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id,
            fighter_a=self.fighter_a,
            fighter_b=self.fighter_b,
            context=self.context,
            verdict=verdict,
            parent_ruling_id=self.parent_ruling_id,
        )


class ChallengeView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Challenge",
        style=discord.ButtonStyle.danger,
        custom_id="fightclub:challenge",
    )
    async def challenge(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
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
                state["verdict"],
                fighter_a=state["fighter_a"],
                fighter_b=state["fighter_b"],
                context=state.get("context"),
                parent_ruling_id=state.get("ruling_id"),
            )
        )


class FightCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="fight", description="Judge a fiction / death-battle matchup")
    @app_commands.describe(
        fighter_a="First fighter / faction",
        fighter_b="Second fighter / faction",
        context="Optional arena, rules, or constraints",
    )
    async def fight(
        self,
        interaction: discord.Interaction,
        fighter_a: str,
        fighter_b: str,
        context: str | None = None,
    ) -> None:
        reject = limiter.check(interaction.user.id, interaction.guild_id)
        if reject:
            await interaction.response.send_message(reject, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        limiter.record(interaction.user.id, interaction.guild_id)
        try:
            verdict = await asyncio.to_thread(judge, fighter_a, fighter_b, context)
        except Exception as e:
            await interaction.followup.send(f"Judgment failed: {e}", ephemeral=True)
            return
        msg = await interaction.followup.send(
            embed=verdict_embed(verdict), view=ChallengeView()
        )
        _persist_ruling(
            message_id=msg.id,
            channel_id=interaction.channel_id,
            guild_id=interaction.guild_id,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            context=context,
            verdict=verdict,
            parent_ruling_id=None,
        )


async def setup(bot: commands.Bot) -> None:
    get_db()  # ensure schema exists at startup
    await bot.add_cog(FightCog(bot))
    bot.add_view(ChallengeView())  # persistent challenge button
