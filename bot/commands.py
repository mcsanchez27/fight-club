"""Slash commands and Challenge button view."""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import verdict_embed
from bot.judge import judge

# last verdict per ruling message id (in-memory only)
_last_verdict: dict[int, dict[str, Any]] = {}


def store_ruling(message_id: int, verdict: dict[str, Any]) -> None:
    """Associate a verdict with the Discord message that displayed it."""
    _last_verdict[message_id] = verdict


def get_ruling(message_id: int) -> dict[str, Any] | None:
    """Look up the verdict for a ruling message, if still in memory."""
    return _last_verdict.get(message_id)


class ChallengeModal(discord.ui.Modal, title="Challenge the ruling"):
    evidence = discord.ui.TextInput(
        label="New evidence / challenge",
        style=discord.TextStyle.paragraph,
        placeholder="Cite canon, logistics, or a win-condition the court missed…",
        required=True,
        max_length=1500,
    )

    def __init__(self, prior: dict[str, Any]):
        super().__init__()
        self.prior = prior

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        matchup = self.prior.get("matchup", "")
        parts = matchup.split(" vs ", 1)
        a = parts[0].strip() if parts else "A"
        b = parts[1].strip() if len(parts) > 1 else "B"
        try:
            verdict = await asyncio.to_thread(
                judge,
                a,
                b,
                None,
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
        store_ruling(msg.id, verdict)


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
        prior = get_ruling(message.id) if message is not None else None
        if not prior:
            await interaction.response.send_message(
                "No ruling attached to this message to challenge. Run `/fight` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ChallengeModal(prior))


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
        await interaction.response.defer(thinking=True)
        try:
            verdict = await asyncio.to_thread(judge, fighter_a, fighter_b, context)
        except Exception as e:
            await interaction.followup.send(f"Judgment failed: {e}", ephemeral=True)
            return
        msg = await interaction.followup.send(
            embed=verdict_embed(verdict), view=ChallengeView()
        )
        store_ruling(msg.id, verdict)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FightCog(bot))
    bot.add_view(ChallengeView())  # persistent challenge button
