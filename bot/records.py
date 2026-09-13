"""V2 item 9 — derived W/L records, leaderboard, flare (never stored)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.db import CourtDB

# Ruling kinds that count toward records (latest per fight wins).
_EXCLUDED_STATUSES = frozenset({"voided", "expired"})


@dataclass(frozen=True)
class FightOutcome:
    """One countable result for two advocates."""

    fight_id: int
    guild_id: int
    result_at: str
    winner_id: int
    loser_id: int
    source: str  # "ruling" | "forfeit"


@dataclass(frozen=True)
class UserRecord:
    user_id: int
    wins: int
    losses: int
    streak: int  # length of current streak (>= 0)
    streak_kind: str  # "W" | "L" | ""
    last5: tuple[str, ...]  # most recent first, each "W" or "L"

    @property
    def win_pct(self) -> float:
        total = self.wins + self.losses
        if total <= 0:
            return 0.0
        return self.wins / total


def _other_advocate(fight: dict[str, Any], user_id: int) -> int | None:
    a = fight.get("advocate_a_id")
    b = fight.get("advocate_b_id")
    if a is None or b is None:
        return None
    a_i, b_i = int(a), int(b)
    uid = int(user_id)
    if uid == a_i:
        return b_i
    if uid == b_i:
        return a_i
    return None


def _fight_excluded(fight: dict[str, Any]) -> bool:
    if bool(fight.get("instant")):
        return True
    status = str(fight.get("status") or "")
    return status in _EXCLUDED_STATUSES


def iter_guild_outcomes(db: CourtDB, guild_id: int) -> list[FightOutcome]:
    """Derive countable outcomes for a guild (rulings + forfeits).

    Records are never stored: latest ``initial``/``reconsideration`` ruling per
    fight; forfeits use ``forfeited_by`` (L for that advocate, W for the other).
    Voided / expired / instant fights are excluded. V1 rows without ``fight_id``
    do not count.
    """
    outcomes: list[FightOutcome] = []
    seen_fights: set[int] = set()

    # --- Ruling-derived (latest initial/reconsideration per fight) ----------
    rows = db._conn.execute(
        """
        SELECT r.id AS ruling_id, r.fight_id, r.kind, r.winner_advocate_id,
               r.winner_side, r.created_at AS ruling_created_at,
               f.guild_id, f.status, f.instant, f.advocate_a_id, f.advocate_b_id,
               f.ruled_at, f.forfeited_by, f.forfeited_at
        FROM rulings r
        INNER JOIN fights f ON f.id = r.fight_id
        WHERE f.guild_id = ?
          AND r.fight_id IS NOT NULL
          AND r.kind IN ('initial', 'reconsideration')
        ORDER BY r.fight_id ASC, r.id DESC
        """,
        (int(guild_id),),
    ).fetchall()

    for row in rows:
        fid = int(row["fight_id"])
        if fid in seen_fights:
            continue
        seen_fights.add(fid)
        fight_like = {
            "instant": bool(row["instant"] or 0),
            "status": row["status"],
            "advocate_a_id": row["advocate_a_id"],
            "advocate_b_id": row["advocate_b_id"],
        }
        if _fight_excluded(fight_like):
            continue
        winner_id = row["winner_advocate_id"]
        if winner_id is None:
            side = (row["winner_side"] or "").strip().lower()
            if side == "a" and row["advocate_a_id"] is not None:
                winner_id = int(row["advocate_a_id"])
            elif side == "b" and row["advocate_b_id"] is not None:
                winner_id = int(row["advocate_b_id"])
        if winner_id is None:
            continue
        winner_id = int(winner_id)
        loser_id = _other_advocate(fight_like, winner_id)
        if loser_id is None:
            continue
        result_at = row["ruled_at"] or row["ruling_created_at"] or ""
        outcomes.append(
            FightOutcome(
                fight_id=fid,
                guild_id=int(row["guild_id"]),
                result_at=str(result_at),
                winner_id=winner_id,
                loser_id=int(loser_id),
                source="ruling",
            )
        )

    # --- Forfeit-derived ----------------------------------------------------
    forfeit_rows = db._conn.execute(
        """
        SELECT id, guild_id, status, instant, advocate_a_id, advocate_b_id,
               forfeited_by, forfeited_at, accepted_at, created_at, ruled_at
        FROM fights
        WHERE guild_id = ?
          AND status = 'forfeited'
        ORDER BY id ASC
        """,
        (int(guild_id),),
    ).fetchall()

    for row in forfeit_rows:
        fid = int(row["id"])
        if fid in seen_fights:
            continue
        fight_like = {
            "instant": bool(row["instant"] or 0),
            "status": row["status"],
            "advocate_a_id": row["advocate_a_id"],
            "advocate_b_id": row["advocate_b_id"],
        }
        if bool(fight_like["instant"]):
            continue
        forfeited_by = row["forfeited_by"]
        if forfeited_by is None:
            continue
        loser_id = int(forfeited_by)
        winner_id = _other_advocate(fight_like, loser_id)
        if winner_id is None:
            continue
        result_at = (
            row["forfeited_at"]
            or row["ruled_at"]
            or row["accepted_at"]
            or row["created_at"]
            or ""
        )
        outcomes.append(
            FightOutcome(
                fight_id=fid,
                guild_id=int(row["guild_id"]),
                result_at=str(result_at),
                winner_id=int(winner_id),
                loser_id=loser_id,
                source="forfeit",
            )
        )

    outcomes.sort(key=lambda o: (o.result_at, o.fight_id))
    return outcomes


def outcomes_for_user(
    outcomes: list[FightOutcome], user_id: int
) -> list[tuple[str, FightOutcome]]:
    """Return chronologically ordered (W|L, outcome) pairs for ``user_id``."""
    uid = int(user_id)
    out: list[tuple[str, FightOutcome]] = []
    for o in outcomes:
        if o.winner_id == uid:
            out.append(("W", o))
        elif o.loser_id == uid:
            out.append(("L", o))
    return out


def compute_streak(results: list[str]) -> tuple[int, str]:
    """Current streak from chronological results (oldest → newest).

    Returns ``(length, kind)`` where kind is ``"W"``, ``"L"``, or ``""``.
    """
    if not results:
        return 0, ""
    kind = results[-1]
    if kind not in {"W", "L"}:
        return 0, ""
    n = 0
    for r in reversed(results):
        if r != kind:
            break
        n += 1
    return n, kind


def build_user_record(user_id: int, results_chrono: list[str]) -> UserRecord:
    wins = sum(1 for r in results_chrono if r == "W")
    losses = sum(1 for r in results_chrono if r == "L")
    streak, kind = compute_streak(results_chrono)
    last5 = tuple(reversed(results_chrono[-5:]))  # most recent first
    return UserRecord(
        user_id=int(user_id),
        wins=wins,
        losses=losses,
        streak=streak,
        streak_kind=kind,
        last5=last5,
    )


def record_for_user(db: CourtDB, guild_id: int, user_id: int) -> UserRecord:
    outcomes = iter_guild_outcomes(db, guild_id)
    pairs = outcomes_for_user(outcomes, user_id)
    return build_user_record(user_id, [p[0] for p in pairs])


def leaderboard(
    db: CourtDB, guild_id: int, *, limit: int = 15
) -> list[UserRecord]:
    """Top ``limit`` advocates by wins, then win%, then user_id."""
    outcomes = iter_guild_outcomes(db, guild_id)
    user_ids: set[int] = set()
    for o in outcomes:
        user_ids.add(o.winner_id)
        user_ids.add(o.loser_id)
    records = [
        build_user_record(uid, [p[0] for p in outcomes_for_user(outcomes, uid)])
        for uid in user_ids
    ]
    records = [r for r in records if (r.wins + r.losses) > 0]
    records.sort(key=lambda r: (-r.wins, -r.win_pct, r.user_id))
    return records[: max(0, int(limit))]


def format_flare_line(
    name: str,
    wins: int,
    losses: int,
    streak: int,
    streak_kind: str,
) -> str:
    """Flare for the ruling embed: ``Name moves to 5-2, 3-fight win streak.``"""
    display = (name or "?").strip() or "?"
    kind_word = "win" if streak_kind == "W" else "loss"
    if streak <= 0 or streak_kind not in {"W", "L"}:
        return f"{display} moves to {wins}-{losses}."
    return (
        f"{display} moves to {wins}-{losses}, "
        f"{streak}-fight {kind_word} streak."
    )


def flare_for_winner(
    db: CourtDB,
    *,
    guild_id: int,
    winner_advocate_id: int,
    winner_name: str,
) -> str:
    """Compute flare line from derived record after a ruling is persisted."""
    rec = record_for_user(db, guild_id, winner_advocate_id)
    return format_flare_line(
        winner_name, rec.wins, rec.losses, rec.streak, rec.streak_kind
    )


def format_streak_label(streak: int, streak_kind: str) -> str:
    if streak <= 0 or streak_kind not in {"W", "L"}:
        return "—"
    word = "W" if streak_kind == "W" else "L"
    return f"{streak}{word}"


def format_last5(last5: tuple[str, ...] | list[str]) -> str:
    if not last5:
        return "—"
    return " ".join(last5)
