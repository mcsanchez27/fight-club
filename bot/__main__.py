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
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready() -> None:
        print(f"Logged in as {bot.user} (id={bot.user.id if bot.user else '?'})")
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
