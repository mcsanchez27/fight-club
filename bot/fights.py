"""V2 fight helpers — deadlines, Accept/Decline/Counter, balance, accept receipts (4a–5).

Pure functions take an explicit ``now`` so tests can freeze the clock.
Do not rely on ``tasks.loop`` for correctness (amendment / C3).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal

from bot.db import CourtDB
from bot.sources import normalize_franchise_key

DEFAULT_CHALLENGE_TIMEOUT_HOURS = 6.0
DEFAULT_COUNTERS_PER_SIDE = 2
DEFAULT_BALANCE_WARN_BELOW = 4.0
DEFAULT_BALANCE_FREE_COUNTER = True
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


def counters_per_side(db: CourtDB, guild_id: int | None) -> int:
    """guild_config override, else env, else 2 (§7)."""
    if guild_id is not None:
        raw = db.get_guild_config(guild_id, "counters_per_side")
        if raw is not None and str(raw).strip():
            try:
                return max(0, int(raw))
            except ValueError:
                pass
    env = os.getenv("FIGHT_COUNTERS_PER_SIDE")
    if env is not None and str(env).strip():
        try:
            return max(0, int(env))
        except ValueError:
            pass
    return DEFAULT_COUNTERS_PER_SIDE


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off", ""}:
        return False
    return False


def balance_warn_below(db: CourtDB | None, guild_id: int | None) -> float:
    """guild_config override, else env, else 4 (§7). Score 1–10, 10=even."""
    if db is not None and guild_id is not None:
        raw = db.get_guild_config(guild_id, "balance_warn_below")
        if raw is not None and str(raw).strip():
            try:
                return float(raw)
            except ValueError:
                pass
    env = os.getenv("FIGHT_BALANCE_WARN_BELOW")
    if env is not None and str(env).strip():
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_BALANCE_WARN_BELOW


def balance_free_counter_enabled(db: CourtDB | None, guild_id: int | None) -> bool:
    """guild_config override, else env, else True (free counter when warned)."""
    if db is not None and guild_id is not None:
        raw = db.get_guild_config(guild_id, "balance_free_counter")
        if raw is not None and str(raw).strip():
            return _as_bool(raw)
    env = os.getenv("FIGHT_BALANCE_FREE_COUNTER")
    if env is not None and str(env).strip():
        return _as_bool(env)
    return DEFAULT_BALANCE_FREE_COUNTER


def compute_expires_at(now: datetime | str, hours: float) -> str:
    dt = as_datetime(now)
    return to_iso(dt + timedelta(hours=float(hours)))


def button_holder_id(fight: dict[str, Any]) -> int | None:
    """Who must Accept / Decline / Counter next (always the challengee)."""
    cid = fight.get("challengee_id")
    return int(cid) if cid is not None else None


def actor_advocate_side(fight: dict[str, Any], actor_id: int) -> Literal["a", "b"] | None:
    """Return ``'a'`` / ``'b'`` if actor is an advocate, else None."""
    aa = fight.get("advocate_a_id")
    ab = fight.get("advocate_b_id")
    if aa is not None and int(actor_id) == int(aa):
        return "a"
    if ab is not None and int(actor_id) == int(ab):
        return "b"
    return None


def sides_complete(fight: dict[str, Any]) -> bool:
    """True when both champion labels are non-empty (needed before balance)."""
    a = fight.get("side_a")
    b = fight.get("side_b")
    return bool(a and str(a).strip()) and bool(b and str(b).strip())


def counter_button_label(
    fight: dict[str, Any] | None = None,
    db: CourtDB | None = None,
) -> str:
    """Label for the Counter button.

    ``Counter (free)`` when ``balance_warned`` and ``balance_free_counter``.
    """
    if fight and is_balance_free_counter_eligible(fight, db=db):
        return "Counter (free)"
    return "Counter"


def is_balance_free_counter_eligible(
    fight: dict[str, Any],
    db: CourtDB | None = None,
) -> bool:
    """True when a counter should not consume the per-side quota.

    Requires ``balance_warned`` and guild/env ``balance_free_counter``.
    """
    if not fight or not fight.get("balance_warned"):
        return False
    explicit = fight.get("balance_free_counter")
    if explicit is not None and db is None:
        return _as_bool(explicit)
    guild_id = fight.get("guild_id")
    return balance_free_counter_enabled(
        db, int(guild_id) if guild_id is not None else None
    )


MISSING_FIGHT_PROMPT = (
    "Need more to start a fight. Provide `opponent:@user` and "
    '`matchup:"A vs B"` for a challenge card, or `opponent` + `side:"Champion"` '
    "for an open-ended challenge, or set `instant:true` with a matchup "
    "(or `fighter_a` + `fighter_b`) for a solo ruling. "
    "No menus — fill the slash fields and run `/fight` again."
)

OPEN_ENDED_SIDE_PROMPT = (
    "Open-ended challenges need `opponent:@user` and your champion in "
    '`side:"Name"`. The challengee names their champion on Accept.'
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
    Open-ended (side without matchup) → proposed card with ``open_ended`` (4b).
    """
    ctx = context.strip() if context and context.strip() else None
    fa = fighter_a.strip() if fighter_a and fighter_a.strip() else None
    fb = fighter_b.strip() if fighter_b and fighter_b.strip() else None
    side_tok = side.strip() if side and side.strip() else None
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

    # Proposed challenge card: opponent + parseable matchup.
    if opponent_id is not None and parsed is not None:
        try:
            side_a, side_b = resolve_sides(matchup or "", side_tok)
        except ValueError:
            return FightCommandPlan(kind="prompt", prompt=MISSING_FIGHT_PROMPT)
        return FightCommandPlan(
            kind="proposed",
            side_a=side_a,
            side_b=side_b,
            context=ctx,
            open_ended=False,
        )

    # Open-ended (4b / lead lock A1): opponent + side, no full matchup.
    if opponent_id is not None and side_tok and not parsed:
        return FightCommandPlan(
            kind="proposed",
            side_a=side_tok,
            side_b=None,
            context=ctx,
            open_ended=True,
        )

    # Opponent but neither matchup nor side.
    if opponent_id is not None and not side_tok and not parsed:
        return FightCommandPlan(kind="prompt", prompt=OPEN_ENDED_SIDE_PROMPT)

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

    Only the button holder (challengee) may accept. Open-ended fights must have
    both sides filled before accept (modal path). Raises ``ValueError`` on
    illegal transition. Caller supplies ``thread_id`` after creating the thread.
    """
    expire_due_fights(db, now)
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if fight["status"] == "expired":
        raise ValueError("This challenge has expired.")
    if fight["status"] != "proposed":
        raise ValueError(f"Cannot accept a fight in status={fight['status']!r}.")
    holder = button_holder_id(fight)
    if holder is not None and int(actor_id) != int(holder):
        raise ValueError("Only the challenged user can Accept.")
    if not sides_complete(fight):
        raise ValueError(
            "Open-ended challenge: name your champion in the Accept modal first."
        )

    accepted_at = to_iso(as_datetime(now))
    fields: dict[str, Any] = {
        "status": "arguing",
        "accepted_at": accepted_at,
        "open_ended": bool(fight.get("open_ended")),
    }
    # Underdog accepted a lopsided (warned) card — record, never a ruling.
    favored = fight.get("balance_favored")
    if fight.get("balance_warned") and favored in {"a", "b"}:
        underdog = "b" if favored == "a" else "a"
        if actor_advocate_side(fight, actor_id) == underdog:
            fields["underdog_accepted"] = True
    if thread_id is not None:
        fields["thread_id"] = thread_id
    db.update_fight(fight_id, **fields)
    out = db.get_fight(fight_id)
    assert out is not None
    return out


OPENING_REST_LINE = "Each side `/rest` when done."


def format_opening_message(fight: dict[str, Any]) -> str:
    """Bot opening post in the argument thread (Tech Design §1 / §5)."""
    side_a = str(fight.get("side_a") or "?").strip() or "?"
    side_b = str(fight.get("side_b") or "?").strip() or "?"
    context = fight.get("context")
    adv_a = fight.get("advocate_a_id")
    adv_b = fight.get("advocate_b_id")
    lines = [
        f"**Matchup:** {side_a} vs {side_b}",
        f"**Side A:** {side_a}"
        + (f" — <@{int(adv_a)}>" if adv_a is not None else ""),
        f"**Side B:** {side_b}"
        + (f" — <@{int(adv_b)}>" if adv_b is not None else ""),
    ]
    if context is not None and str(context).strip():
        lines.append(f"**Context:** {str(context).strip()}")
    lines.extend(
        [
            "",
            "**Rules:** Argue in this thread. Cite sources when you can.",
            "Advocates only — the bot stays silent until rest.",
            OPENING_REST_LINE,
        ]
    )
    return "\n".join(lines)


def fetch_and_store_accept_receipts(
    db: CourtDB,
    fight_id: int,
    *,
    budget_seconds: float | None = None,
    retrieve_fn: Callable[..., Any] | None = None,
) -> Any:
    """Best-effort receipts at Accept (C2). Never raises; Accept always succeeds.

    Uses ``franchise_a`` / ``franchise_b`` from the fight (balance-normalized).
    Stores whatever landed in ``receipts`` keyed by ``fight_id`` and sets
    ``fights.retrieval_status``. Does **not** create gallery exhibit rows.
    """
    from bot.retrieval import RetrievalResult, retrieve_for_accept

    fight = db.get_fight(fight_id)
    if fight is None:
        return RetrievalResult(
            franchise=None, status="unavailable", receipts=[], exhibits=[]
        )

    fn = retrieve_fn or retrieve_for_accept
    try:
        result = fn(
            str(fight.get("side_a") or ""),
            str(fight.get("side_b") or ""),
            fight.get("context"),
            franchise_a=fight.get("franchise_a"),
            franchise_b=fight.get("franchise_b"),
            budget_seconds=budget_seconds,
        )
    except Exception:
        result = RetrievalResult(
            franchise=fight.get("franchise_a") or fight.get("franchise_b"),
            status="unavailable",
            receipts=[],
            exhibits=[],
        )

    # Persist status + passages; replace any prior rows for this fight.
    try:
        db.clear_fight_receipts(fight_id)
        for p in getattr(result, "receipts", None) or []:
            as_dict = p.to_dict() if hasattr(p, "to_dict") else dict(p)
            db.insert_receipt(
                fight_id=fight_id,
                claim=str(as_dict.get("claim") or ""),
                source_url=as_dict.get("source_url"),
                locator=as_dict.get("locator"),
                snippet=as_dict.get("snippet"),
                verified=bool(as_dict.get("verified")),
                retrieved_at=as_dict.get("retrieved_at"),
                kind=str(as_dict.get("kind") or "receipt"),
                source_title=as_dict.get("source_title") or None,
                retrieval_id=as_dict.get("retrieval_id") or None,
                franchise=getattr(result, "franchise", None),
                side=None,
            )
        db.update_fight(fight_id, retrieval_status=str(result.status))
    except Exception:
        try:
            db.update_fight(fight_id, retrieval_status="unavailable")
        except Exception:
            pass
        if not isinstance(result, RetrievalResult):
            result = RetrievalResult(
                franchise=None, status="unavailable", receipts=[], exhibits=[]
            )
        else:
            result = RetrievalResult(
                franchise=result.franchise,
                status="unavailable",
                receipts=list(result.receipts),
                exhibits=list(result.exhibits),
                retrieval_seconds=result.retrieval_seconds,
            )
    return result


def decline_fight(
    db: CourtDB,
    fight_id: int,
    *,
    now: datetime | str,
    actor_id: int,
) -> dict[str, Any]:
    """``proposed`` → ``voided``. Only the button holder (challengee) may decline."""
    expire_due_fights(db, now)
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if fight["status"] == "expired":
        raise ValueError("This challenge has expired.")
    if fight["status"] != "proposed":
        raise ValueError(f"Cannot decline a fight in status={fight['status']!r}.")
    holder = button_holder_id(fight)
    if holder is not None and int(actor_id) != int(holder):
        raise ValueError("Only the challenged user can Decline.")
    db.update_fight(fight_id, status="voided")
    out = db.get_fight(fight_id)
    assert out is not None
    return out


def fill_open_ended_accept(
    db: CourtDB,
    fight_id: int,
    *,
    now: datetime | str,
    actor_id: int,
    champion: str,
    context: str | None = None,
) -> dict[str, Any]:
    """Fill the missing challengee champion on an open-ended proposed fight.

    Sets ``side_b`` (and optional context overwrite). Does **not** accept —
    caller runs balance (when both sides known) then ``accept_fight``.
    """
    expire_due_fights(db, now)
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if fight["status"] == "expired":
        raise ValueError("This challenge has expired.")
    if fight["status"] != "proposed":
        raise ValueError(f"Cannot fill sides in status={fight['status']!r}.")
    if not fight.get("open_ended"):
        raise ValueError("This challenge is not open-ended.")
    holder = button_holder_id(fight)
    if holder is not None and int(actor_id) != int(holder):
        raise ValueError("Only the challenged user can Accept.")
    name = champion.strip() if champion else ""
    if not name:
        raise ValueError("Name your champion to Accept.")
    fields: dict[str, Any] = {"side_b": name}
    if context is not None and str(context).strip():
        fields["context"] = str(context).strip()
    db.update_fight(fight_id, **fields)
    out = db.get_fight(fight_id)
    assert out is not None
    return out


def apply_balance_to_fight(
    db: CourtDB,
    fight_id: int,
    *,
    balance_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Run balance when both sides are known; store scores + warning flag.

    Deferred until sides complete (lead lock A1) — no one-sided score.
    ``balance_warned`` when ``score < balance_warn_below`` (default 4; 10=even).
    Franchises stored via ``normalize_franchise_key`` (item 3 / Q5).
    """
    fight = db.get_fight(fight_id)
    if fight is None or not sides_complete(fight):
        return None
    if balance_fn is None:
        from bot.judge import judge_balance

        balance_fn = judge_balance
    result = balance_fn(
        str(fight["side_a"]),
        str(fight["side_b"]),
        fight.get("context"),
    )
    favored = result.get("favored_side")
    score = float(result["score"])
    guild_id = fight.get("guild_id")
    threshold = balance_warn_below(
        db, int(guild_id) if guild_id is not None else None
    )
    # Strict below: score == threshold is even enough (see NOTES).
    warned = score < float(threshold)
    fields: dict[str, Any] = {
        "balance_score": score,
        "balance_favored": None if favored in (None, "even") else str(favored),
        "balance_reason": str(result.get("reason") or "") or None,
        "balance_warned": warned,
        "franchise_a": normalize_franchise_key(result.get("franchise_a")),
        "franchise_b": normalize_franchise_key(result.get("franchise_b")),
    }
    db.update_fight(fight_id, **fields)
    return result


def counter_fight(
    db: CourtDB,
    fight_id: int,
    *,
    now: datetime | str,
    actor_id: int,
    matchup: str | None = None,
    context: str | None = None,
    swap_sides: bool = False,
    count_against_limit: bool | None = None,
) -> dict[str, Any]:
    """Apply Counter (amendment 6 / Q1). Stays ``proposed`` unless voided.

    Matrix:
    - Always flips button holder (``challenger_id`` ↔ ``challengee_id``).
    - Optional ``swap_sides``: ``side_a``↔``side_b`` and ``advocate_a``↔``advocate_b``.
    - Matchup / context overwrite **only if non-empty**.
    - Increments the countering advocate's side counter (pre-swap).
    - Resets ``expires_at``.
    - At ``counters_per_side`` → ``voided`` (unless ``count_against_limit=False``
      or a free counter after a balance warning).
    """
    expire_due_fights(db, now)
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if fight["status"] == "expired":
        raise ValueError("This challenge has expired.")
    if fight["status"] != "proposed":
        raise ValueError(f"Cannot counter a fight in status={fight['status']!r}.")
    holder = button_holder_id(fight)
    if holder is None or int(actor_id) != int(holder):
        raise ValueError("Only the button holder can Counter.")

    side = actor_advocate_side(fight, actor_id)
    if side is None:
        raise ValueError("Only an advocate can Counter.")

    guild_id = fight.get("guild_id")
    limit = counters_per_side(db, int(guild_id) if guild_id is not None else None)
    ca = int(fight.get("counters_a") or 0)
    cb = int(fight.get("counters_b") or 0)
    used = ca if side == "a" else cb

    # Free counter after balance warning skips the quota.
    if count_against_limit is None:
        against = not is_balance_free_counter_eligible(fight, db=db)
    else:
        against = bool(count_against_limit)
    if against is False:
        pass
    elif used >= limit:
        db.update_fight(fight_id, status="voided")
        out = db.get_fight(fight_id)
        assert out is not None
        return out

    fields: dict[str, Any] = {}
    if against:
        if side == "a":
            fields["counters_a"] = ca + 1
        else:
            fields["counters_b"] = cb + 1

    # Always flip button holder.
    challenger = fight.get("challenger_id")
    challengee = fight.get("challengee_id")
    fields["challenger_id"] = challengee
    fields["challengee_id"] = challenger

    # Matchup / context overwrite only when non-empty (before optional swap).
    parsed = parse_matchup(matchup)
    if parsed is not None:
        fields["side_a"] = parsed[0]
        fields["side_b"] = parsed[1]
        # Filling both sides clears open-ended.
        fields["open_ended"] = False
    if context is not None and str(context).strip():
        fields["context"] = str(context).strip()

    # Resolve current side/advocate values after matchup overwrite.
    side_a = fields.get("side_a", fight.get("side_a"))
    side_b = fields.get("side_b", fight.get("side_b"))
    adv_a = fight.get("advocate_a_id")
    adv_b = fight.get("advocate_b_id")

    if swap_sides:
        fields["side_a"] = side_b
        fields["side_b"] = side_a
        fields["advocate_a_id"] = adv_b
        fields["advocate_b_id"] = adv_a

    hours = challenge_timeout_hours(
        db, int(guild_id) if guild_id is not None else None
    )
    fields["expires_at"] = compute_expires_at(now, hours)
    fields["status"] = "proposed"

    db.update_fight(fight_id, **fields)
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
    side_a: str | None,
    side_b: str | None,
    context: str | None,
    now: datetime | str,
    open_ended: bool = False,
) -> dict[str, Any]:
    """Insert a ``proposed`` fight with ``expires_at`` from challenge timeout."""
    hours = challenge_timeout_hours(db, guild_id)
    expires_at = compute_expires_at(now, hours)
    fight_id = db.create_fight(
        guild_id=guild_id,
        channel_id=channel_id,
        status="proposed",
        instant=False,
        open_ended=open_ended,
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
