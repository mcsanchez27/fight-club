"""V2 fight helpers — deadlines, validation, Accept/Decline transitions (item 4a).

Pure functions take an explicit ``now`` so tests can freeze the clock.
Do not rely on ``tasks.loop`` for correctness (amendment / C3).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from bot.db import CourtDB

DEFAULT_CHALLENGE_TIMEOUT_HOURS = 6.0
_MATCHUP_SPLIT = re.compile(r"\s+vs\.?\s+", re.IGNORECASE)

FightKind = Literal["proposed", "instant", "prompt"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """UTC ISO-8601 with Z suffix (comparable as text)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def as_datetime(now: datetime | str) -> datetime:
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)
    parsed = parse_iso(now)
    if parsed is None:
        raise ValueError(f"invalid timestamp: {now!r}")
    return parsed


def parse_matchup(matchup: str | None) -> tuple[str, str] | None:
    """Split ``\"A vs B\"`` into (A, B). Returns None if unusable."""
    if not matchup or not str(matchup).strip():
        return None
    parts = _MATCHUP_SPLIT.split(str(matchup).strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right:
        return None
    return left, right


def resolve_sides(matchup: str, side: str | None = None) -> tuple[str, str]:
    """Return (side_a, side_b) with challenger's champion as side_a.

    Challenger is always advocate A at propose time (§2). ``side`` may be
    ``A``/``B`` or a fighter name matching either half of the matchup.
    """
    parsed = parse_matchup(matchup)
    if parsed is None:
        raise ValueError("matchup must look like 'A vs B'")
    left, right = parsed
    if side is None or not str(side).strip():
        return left, right
    token = str(side).strip()
    low = token.lower()
    if low in {"b", "side_b", "side b"} or low == right.lower():
        return right, left
    # A / side_a / left name / anything else → left as A
    return left, right


def challenge_timeout_hours(db: CourtDB, guild_id: int | None) -> float:
    """guild_config override, else env, else 6h (§7)."""
    if guild_id is not None:
        raw = db.get_guild_config(guild_id, "challenge_timeout_hours")
        if raw is not None and str(raw).strip():
            try:
                return float(raw)
            except ValueError:
                pass
    env = os.getenv("FIGHT_CHALLENGE_TIMEOUT_HOURS")
    if env is not None and str(env).strip():
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_CHALLENGE_TIMEOUT_HOURS


def compute_expires_at(now: datetime | str, hours: float) -> str:
    dt = as_datetime(now)
    return to_iso(dt + timedelta(hours=float(hours)))


MISSING_FIGHT_PROMPT = (
    "Need more to start a fight. Provide `opponent:@user` and "
    '`matchup:"A vs B"` for a challenge card, or set `instant:true` with a '
    "matchup (or `fighter_a` + `fighter_b`) for a solo ruling. "
    "No menus — fill the slash fields and run `/fight` again."
)


@dataclass(frozen=True)
class FightCommandPlan:
    """Result of validating `/fight` slash fields (unit-tested, no Discord)."""

    kind: FightKind
    prompt: str | None = None
    side_a: str | None = None
    side_b: str | None = None
    context: str | None = None
    open_ended: bool = False


def validate_fight_fields(
    *,
    opponent_id: int | None = None,
    matchup: str | None = None,
    context: str | None = None,
    side: str | None = None,
    instant: bool = False,
    fighter_a: str | None = None,
    fighter_b: str | None = None,
) -> FightCommandPlan:
    """Classify `/fight` into proposed-card, instant bridge, or ephemeral prompt.

    Amendment 12 / §0: missing fields → text prompt only (no menus).
    Open-ended (side without matchup) is deferred to 4b — prompt for now.
    """
    ctx = context.strip() if context and context.strip() else None
    fa = fighter_a.strip() if fighter_a and fighter_a.strip() else None
    fb = fighter_b.strip() if fighter_b and fighter_b.strip() else None
    parsed = parse_matchup(matchup)

    # V1-compatible instant: explicit instant flag, or legacy fighter_a/b alone.
    legacy_instant = fa is not None and fb is not None and opponent_id is None
    wants_instant = bool(instant) or legacy_instant

    if wants_instant:
        if fa and fb:
            return FightCommandPlan(
                kind="instant", side_a=fa, side_b=fb, context=ctx
            )
        if parsed:
            return FightCommandPlan(
                kind="instant", side_a=parsed[0], side_b=parsed[1], context=ctx
            )
        return FightCommandPlan(kind="prompt", prompt=MISSING_FIGHT_PROMPT)

    # Proposed challenge card (4a): opponent + parseable matchup.
    if opponent_id is not None and parsed is not None:
        try:
            side_a, side_b = resolve_sides(matchup or "", side)
        except ValueError:
            return FightCommandPlan(kind="prompt", prompt=MISSING_FIGHT_PROMPT)
        return FightCommandPlan(
            kind="proposed",
            side_a=side_a,
            side_b=side_b,
            context=ctx,
            open_ended=False,
        )

    # 4b territory: open-ended (side without full matchup) — prompt for 4a.
    if opponent_id is not None and side and not parsed:
        return FightCommandPlan(
            kind="prompt",
            prompt=(
                "Open-ended challenges (side without matchup) land in item 4b. "
                'For now provide matchup:"A vs B" with opponent:@user.'
            ),
        )

    return FightCommandPlan(kind="prompt", prompt=MISSING_FIGHT_PROMPT)


def expire_due_fights(db: CourtDB, now: datetime | str) -> list[int]:
    """Mark ``proposed`` fights with ``expires_at <= now`` as ``expired``.

    Returns fight ids that transitioned. Callable from tests with a frozen clock
    and from `/fight` / button handlers at entry (C3).
    """
    now_dt = as_datetime(now)
    now_iso = to_iso(now_dt)
    expired_ids: list[int] = []
    for fight in db.list_fights(status="proposed"):
        exp = fight.get("expires_at")
        if not exp:
            continue
        exp_dt = parse_iso(str(exp))
        if exp_dt is None:
            # Fall back to lexicographic compare for consistent Z timestamps.
            due = str(exp) <= now_iso
        else:
            due = exp_dt <= now_dt
        if due:
            db.update_fight(int(fight["id"]), status="expired")
            expired_ids.append(int(fight["id"]))
    return expired_ids


def accept_fight(
    db: CourtDB,
    fight_id: int,
    *,
    now: datetime | str,
    actor_id: int,
    thread_id: int | None = None,
) -> dict[str, Any]:
    """``proposed`` → ``accepted`` → ``arguing``; record ``accepted_at``.

    Only the challengee may accept. Raises ``ValueError`` on illegal transition.
    Thread creation is optional (item 5); pass ``thread_id`` when available.
    """
    expire_due_fights(db, now)
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if fight["status"] == "expired":
        raise ValueError("This challenge has expired.")
    if fight["status"] != "proposed":
        raise ValueError(f"Cannot accept a fight in status={fight['status']!r}.")
    challengee = fight.get("challengee_id")
    if challengee is not None and int(actor_id) != int(challengee):
        raise ValueError("Only the challenged user can Accept.")

    accepted_at = to_iso(as_datetime(now))
    fields: dict[str, Any] = {
        "status": "arguing",
        "accepted_at": accepted_at,
    }
    if thread_id is not None:
        fields["thread_id"] = thread_id
    # Brief accepted stamp then arguing is OK per 4a; we set arguing directly
    # after writing accepted_at (single update keeps one source of truth).
    db.update_fight(fight_id, **fields)
    # Ensure callers/tests can observe accepted→arguing intent: status is arguing
    # with accepted_at set (accepted is a transient hop).
    out = db.get_fight(fight_id)
    assert out is not None
    return out


def decline_fight(
    db: CourtDB,
    fight_id: int,
    *,
    now: datetime | str,
    actor_id: int,
) -> dict[str, Any]:
    """``proposed`` → ``voided``. Only the challengee may decline."""
    expire_due_fights(db, now)
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if fight["status"] == "expired":
        raise ValueError("This challenge has expired.")
    if fight["status"] != "proposed":
        raise ValueError(f"Cannot decline a fight in status={fight['status']!r}.")
    challengee = fight.get("challengee_id")
    if challengee is not None and int(actor_id) != int(challengee):
        raise ValueError("Only the challenged user can Decline.")
    db.update_fight(fight_id, status="voided")
    out = db.get_fight(fight_id)
    assert out is not None
    return out


def create_proposed_fight(
    db: CourtDB,
    *,
    guild_id: int | None,
    channel_id: int | None,
    challenger_id: int,
    challengee_id: int,
    side_a: str,
    side_b: str,
    context: str | None,
    now: datetime | str,
) -> dict[str, Any]:
    """Insert a ``proposed`` fight with ``expires_at`` from challenge timeout."""
    hours = challenge_timeout_hours(db, guild_id)
    expires_at = compute_expires_at(now, hours)
    fight_id = db.create_fight(
        guild_id=guild_id,
        channel_id=channel_id,
        status="proposed",
        instant=False,
        open_ended=False,
        challenger_id=challenger_id,
        challengee_id=challengee_id,
        advocate_a_id=challenger_id,
        advocate_b_id=challengee_id,
        side_a=side_a,
        side_b=side_b,
        context=context,
        expires_at=expires_at,
    )
    fight = db.get_fight(fight_id)
    assert fight is not None
    return fight
