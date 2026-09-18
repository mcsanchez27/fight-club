"""Start the Discord Fight Club bot: python -m bot"""

from __future__ import annotations

import asyncio
import os
import sys

import discord
from discord.ext import commands
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print(
            "Error: DISCORD_TOKEN is not set. Copy .env.example to .env and add your token.",
            file=sys.stderr,
        )
        return 1

    intents = discord.Intents.default()
    # Privileged Message Content — required for fight-thread transcripts (amendment 13 / C7).
    # Portal toggle MUST be on: if code requests this intent but the Developer Portal
    # has it disabled, discord.py raises PrivilegedIntentsRequired and the process
    # exits before on_ready (so the warning below never runs). Keep the hard require.
    intents.message_content = True
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        print(f"Logged in as {bot.user} (id={bot.user.id if bot.user else '?'})")
        if not bot.intents.message_content:
            print(
                "WARNING: Message Content intent is not enabled on this connection. "
                "Enable Privileged Message Content in the Developer Portal "
                "(Bot → Privileged Gateway Intents) and keep "
                "Intents.message_content = True in bot/__main__.py. "
                "Without it, thread transcript reading will fail.",
                file=sys.stderr,
            )
        # B7: non-fatal ping of every allowlisted source (debounced in retrieval).
        try:
            from bot.retrieval import log_allowlisted_source_health

            await asyncio.to_thread(log_allowlisted_source_health)
        except Exception as e:
            print(
                f"[sources] health check skipped: {type(e).__name__}",
                file=sys.stderr,
            )
        # Loud warn when allow_self_fight is on anywhere (test harness — do not rot in prod).
        try:
            from bot.config import allow_self_fight_enabled_somewhere
            from bot.db import get_db

            if allow_self_fight_enabled_somewhere(get_db()):
                print(
                    "WARNING: allow_self_fight is ENABLED (env and/or guild_config). "
                    "Challengers can Accept their own cards — test harness only. "
                    "Turn off via `/config allow_self_fight false` and unset "
                    "FIGHT_ALLOW_SELF_FIGHT before treating this as production.",
                    file=sys.stderr,
                )
        except Exception as e:
            print(
                f"[config] allow_self_fight check skipped: {type(e).__name__}",
                file=sys.stderr,
            )
        try:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} app command(s)")
        except Exception as e:
            print(f"Command sync failed: {e}", file=sys.stderr)

    async def runner() -> None:
        async with bot:
            await bot.load_extension("bot.commands")
            await bot.start(token)

    asyncio.run(runner())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
