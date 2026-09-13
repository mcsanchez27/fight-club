"""V2 item 8 — judge_ready → ruling call, persist, drop helpers, thin-record."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable

from bot.db import CourtDB
from bot.exhibits import format_exhibit_ledger, prepare_judge_materials
from bot.fights import (
    JUDGE_READY_STATUS,
    RULED_STATUS,
    as_datetime,
    compute_expires_at,
    fetch_and_store_accept_receipts,
    thread_archive_delay_hours,
    to_iso,
)
from bot.judge import judge_with_materials, ruling_model, validate_verdict
from bot.retrieval import Passage, RetrievalResult
from bot.transcript import snapshot_text

THIN_RECORD_BANNER = "Thin record — ruled on available argument."

# Receipt statuses that warrant one short refresh (Amendment 4 / A2).
_REFRESH_STATUSES = frozenset({"unavailable", "unlisted", "disabled", ""})


def display_name(user_or_name: Any) -> str:
    """Resolve a Discord user-like object or plain string to a display name.

    Tests may pass plain strings; Discord Members/Users use ``display_name``.
    """
    if user_or_name is None:
        return "?"
    if isinstance(user_or_name, str):
        return user_or_name.strip() or "?"
    name = getattr(user_or_name, "display_name", None) or getattr(
        user_or_name, "global_name", None
    ) or getattr(user_or_name, "name", None)
    if name:
        return str(name)
    uid = getattr(user_or_name, "id", None)
    return str(uid) if uid is not None else "?"


def jump_url(
    guild_id: int | None, channel_id: int | None, message_id: int | None
) -> str:
    """Discord jump link (guild/channel/message)."""
    if guild_id is None or channel_id is None or message_id is None:
        return ""
    return f"https://discord.com/channels/{int(guild_id)}/{int(channel_id)}/{int(message_id)}"


def format_winner_one_liner(
    *,
    winner_name: str,
    side_a: str,
    side_b: str,
    jump_link: str,
) -> str:
    """Channel one-liner: trophy + winner + matchup + jump link."""
    name = display_name(winner_name)
    a = (side_a or "?").strip() or "?"
    b = (side_b or "?").strip() or "?"
    link = (jump_link or "").strip()
    if link:
        return f"🏆 **{name}** won *{a} vs {b}* — {link}"
    return f"🏆 **{name}** won *{a} vs {b}*"


def side_message_counts(snapshot: Any) -> dict[str, int]:
    """Count advocate turns per side from a stored transcript snapshot."""
    counts = {"a": 0, "b": 0}
    if not snapshot:
        return counts
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except json.JSONDecodeError:
            return counts
    turns = snapshot.get("turns") if isinstance(snapshot, dict) else None
    if isinstance(turns, list):
        for t in turns:
            if not isinstance(t, dict):
                continue
            side = str(t.get("side") or "").lower()
            if side in counts:
                counts[side] += 1
        return counts
    # Fallback: parse labeled lines.
    text = snapshot_text(snapshot) if isinstance(snapshot, dict) else ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[A:") or s.startswith("[A]"):
            counts["a"] += 1
        elif s.startswith("[B:") or s.startswith("[B]"):
            counts["b"] += 1
    return counts


def is_thin_record(snapshot: Any) -> bool:
    """Amendment 7: either side has <1 advocate message in the snapshot."""
    counts = side_message_counts(snapshot)
    return counts["a"] < 1 or counts["b"] < 1


def format_fight_setup(fight: dict[str, Any]) -> str:
    side_a = str(fight.get("side_a") or "?")
    side_b = str(fight.get("side_b") or "?")
    lines = [
        f"Matchup: {side_a} vs {side_b}",
        f"Side A: {side_a}",
        f"Side B: {side_b}",
    ]
    ctx = fight.get("context")
    if ctx:
        lines.append(f"Context: {ctx}")
    fa = fight.get("franchise_a")
    fb = fight.get("franchise_b")
    if fa or fb:
        lines.append(f"Franchise A: {fa or '—'} · Franchise B: {fb or '—'}")
    return "\n".join(lines)


def pack_db_receipts_for_prompt(
    receipts: list[dict[str, Any]], status: str | None
) -> str:
    """Format fight-keyed receipt rows for the referee prompt slot."""
    st = status or "unavailable"
    lines: list[str] = [f"RETRIEVAL_STATUS: {st}"]
    if st in _REFRESH_STATUSES or st == "unavailable":
        lines.append(
            "Retrieval unavailable or empty. Put missing canon in unknowns "
            "(legal plea). Do NOT invent URLs. Confidence will be capped at 5/10."
        )
    if receipts:
        lines.append(
            "RECEIPTS (autonomous fetch — cite these for load-bearing ruling/concessions):"
        )
        for i, r in enumerate(receipts, start=1):
            rid = r.get("retrieval_id") or f"r{i}"
            loc = r.get("locator") or ""
            url = r.get("source_url") or ""
            snip = r.get("snippet") or ""
            lines.append(f"- [{rid}] {loc} | {url}\n  snippet: {snip}")
    else:
        lines.append("No receipts packed.")
    return "\n".join(lines)


def receipts_to_retrieval_result(
    receipts: list[dict[str, Any]],
    *,
    status: str | None,
    franchise: str | None,
) -> RetrievalResult:
    passages: list[Passage] = []
    for r in receipts:
        passages.append(
            Passage(
                claim=str(r.get("claim") or ""),
                source_url=str(r.get("source_url") or ""),
                locator=str(r.get("locator") or ""),
                snippet=str(r.get("snippet") or ""),
                verified=bool(r.get("verified")),
                retrieved_at=str(r.get("retrieved_at") or ""),
                kind=str(r.get("kind") or "receipt"),
                source_title=str(r.get("source_title") or ""),
                retrieval_id=str(r.get("retrieval_id") or ""),
            )
        )
    st = status or ("ok" if passages else "unavailable")
    return RetrievalResult(
        franchise=franchise,
        status=st,
        receipts=passages,
        exhibits=[],
    )


def ensure_transcript_snapshot(
    db: CourtDB,
    fight: dict[str, Any],
    messages: list | None = None,
    *,
    fetch_url: Any = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Amendment 2: read fights.transcript_snapshot only after it exists.

    If null, build once from ``messages`` (Discord history fetch done by caller),
    store, then thereafter snapshot-only.
    """
    existing = fight.get("transcript_snapshot")
    if existing is not None:
        if isinstance(existing, dict):
            return existing
        if isinstance(existing, str) and existing.strip():
            try:
                return json.loads(existing)
            except json.JSONDecodeError:
                return {"version": 1, "text": existing, "lines": [existing], "turns": []}
        # Empty string / odd value — treat as missing.
    msgs = list(messages) if messages is not None else []
    prepared = prepare_judge_materials(
        db,
        int(fight["id"]),
        msgs,
        fetch_url=fetch_url,
        max_tokens=max_tokens,
    )
    return prepared["snapshot"]


def load_or_refresh_receipts(
    db: CourtDB,
    fight: dict[str, Any],
    *,
    retrieve_fn: Callable[..., Any] | None = None,
    allow_refresh: bool = True,
) -> tuple[list[dict[str, Any]], str, bool]:
    """Reuse accept receipts; one short refresh if empty/unavailable (A2).

    Never refuses — returns whatever is available (House Rule 3).
    Returns ``(receipts, status, did_refresh)``.
    """
    fight_id = int(fight["id"])
    receipts = list(db.list_receipts(fight_id))
    status = str(fight.get("retrieval_status") or "")
    did_refresh = False
    needs = (not receipts) or (status in _REFRESH_STATUSES) or status == "unavailable"
    # Also refresh when status is literally unavailable even if rows exist? Spec:
    # "one short refresh if empty/unavailable"
    if allow_refresh and needs:
        fetch_and_store_accept_receipts(
            db, fight_id, retrieve_fn=retrieve_fn
        )
        did_refresh = True
        fresh = db.get_fight(fight_id) or fight
        receipts = list(db.list_receipts(fight_id))
        status = str(fresh.get("retrieval_status") or status or "unavailable")
        # Never refresh a second time in this call.
    if not status:
        status = "ok" if receipts else "unavailable"
    return receipts, status, did_refresh


def _winner_advocate_id(fight: dict[str, Any], winner_side: str | None) -> int | None:
    if winner_side == "a":
        v = fight.get("advocate_a_id")
        return int(v) if v is not None else None
    if winner_side == "b":
        v = fight.get("advocate_b_id")
        return int(v) if v is not None else None
    return None


def rule_fight(
    db: CourtDB,
    fight_id: int,
    *,
    now: datetime | str,
    messages: list | None = None,
    client: Any | None = None,
    retrieve_fn: Callable[..., Any] | None = None,
    judge_fn: Callable[..., dict[str, Any]] | None = None,
    fetch_url: Any = None,
    allow_receipt_refresh: bool = True,
) -> dict[str, Any]:
    """Run the item-8 judge path for a ``judge_ready`` fight.

    Idempotent: if already ``ruled``, returns the existing state without
    calling the model again.

    Returns a dict with fight, ruling_id, verdict, thin_record, already_ruled,
    snapshot, exhibit_ledger, receipts_refreshed, ruled_at, archive_at.
    """
    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")

    if fight.get("status") == RULED_STATUS:
        existing = db.get_latest_ruling_for_fight(fight_id)
        return {
            "fight": fight,
            "ruling_id": existing["id"] if existing else None,
            "verdict": (existing or {}).get("verdict"),
            "thin_record": is_thin_record(fight.get("transcript_snapshot")),
            "already_ruled": True,
            "snapshot": fight.get("transcript_snapshot"),
            "exhibit_ledger": format_exhibit_ledger(db.list_exhibits(fight_id)),
            "receipts_refreshed": False,
            "ruled_at": fight.get("ruled_at"),
            "archive_at": fight.get("archive_at"),
        }

    if fight.get("status") != JUDGE_READY_STATUS:
        raise ValueError(
            f"Cannot rule a fight in status={fight.get('status')!r} "
            f"(need {JUDGE_READY_STATUS!r})."
        )

    # Amendment 2 — snapshot only after first store.
    snapshot = ensure_transcript_snapshot(
        db, fight, messages, fetch_url=fetch_url
    )
    # Re-read fight (snapshot / exhibits may have been written).
    fight = db.get_fight(fight_id) or fight

    exhibits = db.list_exhibits(fight_id)
    ledger_text = format_exhibit_ledger(exhibits)
    thin = is_thin_record(snapshot)

    receipts, ret_status, refreshed = load_or_refresh_receipts(
        db,
        fight,
        retrieve_fn=retrieve_fn,
        allow_refresh=allow_receipt_refresh,
    )
    fight = db.get_fight(fight_id) or fight

    receipts_text = pack_db_receipts_for_prompt(receipts, ret_status)
    transcript = snapshot_text(snapshot)
    setup = format_fight_setup(fight)
    franchise = fight.get("franchise_a") or fight.get("franchise_b")
    retrieval = receipts_to_retrieval_result(
        receipts, status=ret_status, franchise=franchise if isinstance(franchise, str) else None
    )

    t0 = time.monotonic()
    fn = judge_fn or judge_with_materials
    verdict = fn(
        fight_setup=setup,
        receipts=receipts_text,
        transcript=transcript,
        exhibit_ledger=ledger_text,
        client=client,
        retrieval_result=retrieval,
    )
    # Ensure validation / soft scores even if custom judge_fn skips it.
    verdict = validate_verdict(verdict)
    judge_seconds = time.monotonic() - t0
    usage = dict(verdict.get("_usage") or {})
    usage.setdefault("judge_seconds", round(judge_seconds, 4))
    usage.setdefault("retrieval_seconds", 0.0)
    usage.setdefault(
        "total_seconds",
        round(float(usage.get("judge_seconds", 0)) + float(usage.get("retrieval_seconds", 0)), 4),
    )
    usage["role"] = "ruling"
    usage["model"] = usage.get("model") or ruling_model()
    verdict["_usage"] = usage
    if thin:
        verdict["thin_record"] = True
        verdict["thin_record_banner"] = THIN_RECORD_BANNER

    winner_side = verdict.get("winner_side")
    if isinstance(winner_side, str):
        winner_side = winner_side.strip().lower()
    winner_adv = _winner_advocate_id(fight, winner_side)

    now_dt = as_datetime(now)
    ruled_at = to_iso(now_dt)
    guild_id = fight.get("guild_id")
    try:
        guild_i = int(guild_id) if guild_id is not None else None
    except (TypeError, ValueError):
        guild_i = None
    delay_h = thread_archive_delay_hours(db, guild_i)
    archive_at = compute_expires_at(now_dt, delay_h)

    # Copy snapshot onto the ruling row (Amendment 2 staging → permanent).
    snap_copy = snapshot
    if isinstance(snap_copy, dict):
        snap_copy = json.loads(json.dumps(snap_copy))  # deep copy via JSON

    matchup = f"{fight.get('side_a') or '?'} vs {fight.get('side_b') or '?'}"
    verdict["matchup"] = matchup
    # Winner display text for embeds (side name — prefer over V1 letter).
    if winner_side in {"a", "b"}:
        side_name = str(fight.get(f"side_{winner_side}") or winner_side.upper())
        verdict["winner"] = side_name

    ruling_id = db.insert_ruling(
        message_id=None,
        channel_id=fight.get("thread_id") or fight.get("channel_id"),
        guild_id=fight.get("guild_id"),
        fighter_a=str(fight.get("side_a") or ""),
        fighter_b=str(fight.get("side_b") or ""),
        context=fight.get("context"),
        verdict=verdict,
        parent_ruling_id=None,
        franchise=franchise if isinstance(franchise, str) else None,
        retrieval_status=ret_status,
        voided=bool(verdict.get("voided")),
        fight_id=fight_id,
        kind="initial",
        judge_model=str(usage.get("model") or ruling_model()),
        winner_side=winner_side if winner_side in {"a", "b"} else None,
        winner_advocate_id=winner_adv,
        confidence=float(verdict["confidence"]) if verdict.get("confidence") is not None else None,
        opening_score_a=verdict.get("opening_score_a"),
        opening_score_b=verdict.get("opening_score_b"),
        close_score_a=verdict.get("close_score_a"),
        close_score_b=verdict.get("close_score_b"),
        argument_quality=verdict.get("argument_quality"),
        transcript_snapshot=snap_copy,
    )

    # Persist citations on the ruling (V1 table).
    for c in verdict.get("citations") or []:
        if isinstance(c, str):
            db.insert_citation(
                ruling_id=ruling_id,
                claim=c,
                source_url=None,
                locator=None,
                snippet=None,
                verified=False,
                retrieved_at=None,
                kind="receipt",
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

    db.update_fight(
        fight_id,
        status=RULED_STATUS,
        ruled_at=ruled_at,
        archive_at=archive_at,
    )
    fight = db.get_fight(fight_id) or fight

    # Usage bookkeeping (best-effort).
    try:
        from bot.budget import current_month, estimate_usd, usage_from_verdict

        u = usage_from_verdict(verdict)
        tin = u["tokens_in"]
        tout = u["tokens_out"]
        if tin <= 0 and tout <= 0:
            tin = 5000 if ret_status == "ok" else 2500
            tout = 1000
        db.record_usage(
            month=current_month(),
            tokens_in=tin,
            tokens_out=tout,
            estimated_usd=estimate_usd(tin, tout),
            retrieval_seconds=u["retrieval_seconds"],
            judge_seconds=u["judge_seconds"],
            total_seconds=u["total_seconds"],
            fight_id=fight_id,
            role="ruling",
        )
    except Exception:
        pass

    return {
        "fight": fight,
        "ruling_id": ruling_id,
        "verdict": verdict,
        "thin_record": thin,
        "already_ruled": False,
        "snapshot": snapshot,
        "exhibit_ledger": ledger_text,
        "receipts_refreshed": refreshed,
        "ruled_at": ruled_at,
        "archive_at": archive_at,
    }
