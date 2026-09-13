"""Per-user cooldown and per-guild daily judge-call caps."""

from __future__ import annotations

import time
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.db import CourtDB


def cooldown_seconds(db: CourtDB | None = None, guild_id: int | None = None) -> int:
    from bot.config import get_guild_config

    return max(0, int(get_guild_config(db, guild_id, "cooldown_seconds")))


def guild_daily_cap(db: CourtDB | None = None, guild_id: int | None = None) -> int:
    from bot.config import get_guild_config

    return max(0, int(get_guild_config(db, guild_id, "daily_cap")))


class RateLimiter:
    """In-memory per-user cooldown + per-guild daily judge cap."""

    def __init__(self) -> None:
        self._user_last: dict[int, float] = {}
        self._guild_day_counts: dict[tuple[int, str], int] = {}

    def clear(self) -> None:
        self._user_last.clear()
        self._guild_day_counts.clear()

    def check(
        self,
        user_id: int,
        guild_id: int | None,
        db: CourtDB | None = None,
    ) -> str | None:
        """Return an ephemeral rejection message, or None if allowed."""
        now = time.time()
        cd = cooldown_seconds(db, guild_id)
        last = self._user_last.get(user_id)
        if last is not None and cd > 0:
            remaining = cd - (now - last)
            if remaining > 0:
                return (
                    f"Cooldown active — try again in {int(remaining) + 1}s "
                    f"(limit {cd}s per user)."
                )

        if guild_id is not None:
            cap = guild_daily_cap(db, guild_id)
            key = (guild_id, date.today().isoformat())
            used = self._guild_day_counts.get(key, 0)
            if cap > 0 and used >= cap:
                return (
                    f"This server has hit today's judge cap ({cap} rulings). "
                    "Try again tomorrow."
                )
        return None

    def record(self, user_id: int, guild_id: int | None) -> None:
        """Record a successful judge-call attempt (after check passed)."""
        self._user_last[user_id] = time.time()
        if guild_id is not None:
            key = (guild_id, date.today().isoformat())
            self._guild_day_counts[key] = self._guild_day_counts.get(key, 0) + 1


limiter = RateLimiter()
