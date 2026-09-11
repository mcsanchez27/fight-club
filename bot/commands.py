"""Slash commands and Challenge button view."""

from __future__ import annotations

import asyncio
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.embeds import verdict_embed
from bot.judge import judge

# ruling state per ruling message id (in-memory only)
# value: {"verdict", "fighter_a", "fighter_b", "context"}
_last_verdict: dict[int, dict[str, Any]] = {}


def store_ruling(
    message_id: int,
    verdict: dict[str, Any],
    *,
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
) -> None:
    """Associate a verdict and matchup fields with the Discord message that displayed it."""
    _last_verdict[message_id] = {
        "verdict": verdict,
        "fighter_a": fighter_a,
        "fighter_b": fighter_b,
        "context": context,
    }


def get_ruling(message_id: int) -> dict[str, Any] | None:
    """Look up stored ruling state for a message, if still in memory."""
    return _last_verdict.get(message_id)


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
    ):
        super().__init__()
        self.prior = prior
        self.fighter_a = fighter_a
        self.fighter_b = fighter_b
        self.context = context

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
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
        store_ruling(
            msg.id,
            verdict,
            fighter_a=self.fighter_a,
            fighter_b=self.fighter_b,
            context=self.context,
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
        await interaction.response.defer(thinking=True)
        try:
            verdict = await asyncio.to_thread(judge, fighter_a, fighter_b, context)
        except Exception as e:
            await interaction.followup.send(f"Judgment failed: {e}", ephemeral=True)
            return
        msg = await interaction.followup.send(
            embed=verdict_embed(verdict), view=ChallengeView()
        )
        store_ruling(
            msg.id,
            verdict,
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            context=context,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FightCog(bot))
    bot.add_view(ChallengeView())  # persistent challenge button
