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


# --- V2 item 10: /reconsider -------------------------------------------------

def format_confidence(value: Any) -> str:
    """One-decimal confidence for diff strings (e.g. 7 → ``7.0``)."""
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "?"


def reconsideration_diff(
    prior_winner_side: str | None,
    prior_confidence: Any,
    new_winner_side: str | None,
    new_confidence: Any,
) -> str:
    """One-line diff for the reconsideration embed.

    ``Winner unchanged, confidence X → Y`` or ``Winner reversed``.
    """
    prior_side = (prior_winner_side or "").strip().lower() or None
    new_side = (new_winner_side or "").strip().lower() or None
    if prior_side in {"a", "b"} and new_side in {"a", "b"} and prior_side != new_side:
        return "Winner reversed"
    return (
        f"Winner unchanged, confidence "
        f"{format_confidence(prior_confidence)} → {format_confidence(new_confidence)}"
    )


def format_prior_ruling_block(
    prior: dict[str, Any],
    evidence: str,
) -> str:
    """Fill the referee ``{{PRIOR_RULING}}`` slot for reconsideration."""
    verdict = prior.get("verdict") if isinstance(prior.get("verdict"), dict) else {}
    winner_side = prior.get("winner_side") or verdict.get("winner_side")
    confidence = prior.get("confidence")
    if confidence is None:
        confidence = verdict.get("confidence")
    winner = verdict.get("winner") or winner_side or "?"
    ruling_text = str(verdict.get("ruling") or "").strip()
    lines = [
        "PRIOR RULING (original — still on the record):",
        f"- kind: {prior.get('kind') or 'initial'}",
        f"- ruling_id: {prior.get('id')}",
        f"- winner_side: {winner_side}",
        f"- winner: {winner}",
        f"- confidence: {format_confidence(confidence)}",
    ]
    if ruling_text:
        lines.append(f"- ruling: {ruling_text}")
    steel_a = str(verdict.get("steelman_a") or "").strip()
    steel_b = str(verdict.get("steelman_b") or "").strip()
    if steel_a:
        lines.append(f"- steelman_a: {steel_a}")
    if steel_b:
        lines.append(f"- steelman_b: {steel_b}")
    concessions = verdict.get("concessions") or []
    if concessions:
        lines.append("- concessions: " + "; ".join(str(c) for c in concessions))
    lines.append("")
    lines.append("NEW EVIDENCE (motion for reconsideration):")
    lines.append(evidence.strip())
    lines.append("")
    lines.append(
        "Re-judge the *same transcript snapshot* in light of this evidence. "
        "Both the original and this reconsideration stay on the record."
    )
    return "\n".join(lines)


def fight_has_reconsideration(db: CourtDB, fight_id: int) -> bool:
    """True when a ``kind='reconsideration'`` ruling already exists for the fight."""
    return db.fight_has_reconsideration(int(fight_id))


def get_original_ruling_for_fight(db: CourtDB, fight_id: int) -> dict[str, Any] | None:
    """Earliest ``initial`` ruling for the fight (parent for reconsideration)."""
    return db.get_initial_ruling_for_fight(int(fight_id))


def resolve_reconsider_snapshot(
    fight: dict[str, Any],
    prior: dict[str, Any],
) -> Any:
    """Amendment 2: snapshot only — prior ruling first, else fight staging.

    Never fetches live Discord history.
    """
    for source in (prior.get("transcript_snapshot"), fight.get("transcript_snapshot")):
        if source is None:
            continue
        if isinstance(source, str) and not source.strip():
            continue
        if isinstance(source, str):
            try:
                return json.loads(source)
            except json.JSONDecodeError:
                return {"version": 1, "text": source, "lines": [source], "turns": []}
        return source
    return None


def reconsider_fight(
    db: CourtDB,
    fight_id: int,
    *,
    evidence: str,
    now: datetime | str,
    actor_id: int | None = None,
    client: Any | None = None,
    retrieve_fn: Callable[..., Any] | None = None,
    judge_fn: Callable[..., dict[str, Any]] | None = None,
    allow_receipt_refresh: bool = True,
) -> dict[str, Any]:
    """V2 item 10 — once-per-fight reconsideration on a ``ruled`` fight.

    Snapshot-only (amendment 2). Receipts: reuse accept rows + one-shot refresh
    if empty (A2). Inserts ``kind='reconsideration'`` linked via
    ``parent_ruling_id``; status stays ``ruled``.
    """
    evidence_s = (evidence or "").strip()
    if not evidence_s:
        raise ValueError("Evidence is required for /reconsider (non-empty string).")

    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")

    if fight.get("status") != RULED_STATUS:
        raise ValueError(
            f"Cannot reconsider a fight in status={fight.get('status')!r} "
            f"(need {RULED_STATUS!r})."
        )

    if actor_id is not None:
        from bot.fights import actor_advocate_side

        if actor_advocate_side(fight, int(actor_id)) is None:
            raise ValueError("Only advocates can /reconsider.")

    if fight_has_reconsideration(db, fight_id):
        raise ValueError(
            "This fight already has a reconsideration — only one /reconsider per fight."
        )

    prior = get_original_ruling_for_fight(db, fight_id)
    if prior is None:
        raise ValueError("No original ruling found for this fight.")

    snapshot = resolve_reconsider_snapshot(fight, prior)
    if snapshot is None:
        raise ValueError(
            "No transcript_snapshot on the prior ruling or fight — "
            "cannot reconsider without the frozen record."
        )

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
    prior_block = format_prior_ruling_block(prior, evidence_s)
    franchise = fight.get("franchise_a") or fight.get("franchise_b")
    retrieval = receipts_to_retrieval_result(
        receipts,
        status=ret_status,
        franchise=franchise if isinstance(franchise, str) else None,
    )

    user_msg = (
        "This is a MOTION FOR RECONSIDERATION. Re-deliver the verdict by calling "
        "deliver_verdict once. Steelman both sides before the ruling. Weigh the "
        "new evidence against the same transcript snapshot and the prior ruling "
        "attached in the system prompt. Opening/close/argument_quality may be null "
        "if the record is thin."
    )

    t0 = time.monotonic()
    fn = judge_fn or judge_with_materials
    verdict = fn(
        fight_setup=setup,
        receipts=receipts_text,
        transcript=transcript,
        exhibit_ledger=ledger_text,
        prior_ruling=prior_block,
        user_msg=user_msg,
        client=client,
        retrieval_result=retrieval,
    )
    verdict = validate_verdict(verdict)
    judge_seconds = time.monotonic() - t0
    usage = dict(verdict.get("_usage") or {})
    usage.setdefault("judge_seconds", round(judge_seconds, 4))
    usage.setdefault("retrieval_seconds", 0.0)
    usage.setdefault(
        "total_seconds",
        round(
            float(usage.get("judge_seconds", 0))
            + float(usage.get("retrieval_seconds", 0)),
            4,
        ),
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

    prior_winner = prior.get("winner_side")
    if not prior_winner and isinstance(prior.get("verdict"), dict):
        prior_winner = prior["verdict"].get("winner_side")
    prior_conf = prior.get("confidence")
    if prior_conf is None and isinstance(prior.get("verdict"), dict):
        prior_conf = prior["verdict"].get("confidence")
    new_conf = verdict.get("confidence")
    diff_line = reconsideration_diff(prior_winner, prior_conf, winner_side, new_conf)
    verdict["reconsideration_diff"] = diff_line

    matchup = f"{fight.get('side_a') or '?'} vs {fight.get('side_b') or '?'}"
    verdict["matchup"] = matchup
    if winner_side in {"a", "b"}:
        side_name = str(fight.get(f"side_{winner_side}") or winner_side.upper())
        verdict["winner"] = side_name

    snap_copy = snapshot
    if isinstance(snap_copy, dict):
        snap_copy = json.loads(json.dumps(snap_copy))

    ruling_id = db.insert_ruling(
        message_id=None,
        channel_id=fight.get("thread_id") or fight.get("channel_id"),
        guild_id=fight.get("guild_id"),
        fighter_a=str(fight.get("side_a") or ""),
        fighter_b=str(fight.get("side_b") or ""),
        context=fight.get("context"),
        verdict=verdict,
        parent_ruling_id=int(prior["id"]),
        franchise=franchise if isinstance(franchise, str) else None,
        retrieval_status=ret_status,
        voided=bool(verdict.get("voided")),
        fight_id=fight_id,
        kind="reconsideration",
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

    # Status stays ruled — do not rewrite ruled_at / archive_at.
    fight = db.get_fight(fight_id) or fight
    assert fight.get("status") == RULED_STATUS

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
        "parent_ruling_id": int(prior["id"]),
        "verdict": verdict,
        "thin_record": thin,
        "snapshot": snapshot,
        "exhibit_ledger": ledger_text,
        "receipts_refreshed": refreshed,
        "diff_line": diff_line,
        "prior_ruling": prior,
        "evidence": evidence_s,
        "kind": "reconsideration",
    }


# --- Item 12: instant rulings on fight rows ------------------------------------

CHALLENGE_ONLY_INSTANT_MSG = (
    "Challenge is only available on **instant** rulings. "
    "For thread fights, use `/reconsider` with new evidence."
)


def resolve_instant_winner_side(
    verdict: dict[str, Any],
    side_a: str | None,
    side_b: str | None,
) -> str | None:
    """Map verdict winner_side / free-text winner onto ``'a'|'b'`` when possible."""
    ws = verdict.get("winner_side")
    if isinstance(ws, str) and ws.strip().lower() in {"a", "b"}:
        return ws.strip().lower()
    w = verdict.get("winner")
    if not isinstance(w, str) or not w.strip():
        return None
    wl = w.strip().lower()
    if wl in {"a", "b"}:
        return wl
    if side_a and wl == str(side_a).strip().lower():
        return "a"
    if side_b and wl == str(side_b).strip().lower():
        return "b"
    return None


def challenge_allowed_for_ruling(
    ruling: dict[str, Any] | None,
    *,
    fight: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Whether the V1 Challenge button may open for this ruling (§3 / §11.12).

    Allowed: ``kind='instant'``, ``fight.instant``, or legacy V1 rows (no
    ``fight_id`` / kind). Rejected: thread ``initial``/``reconsideration``
    rulings — callers should point users at ``/reconsider``.
    """
    if not ruling:
        return False, (
            "No ruling attached to this message to challenge. Run `/fight` first."
        )
    kind_raw = ruling.get("kind")
    kind = str(kind_raw).strip().lower() if kind_raw is not None else None
    if kind == "":
        kind = None
    fight_id = ruling.get("fight_id")

    if kind == "instant":
        return True, None
    if fight is not None and bool(fight.get("instant")):
        return True, None
    if kind in {"initial", "reconsideration"}:
        return False, CHALLENGE_ONLY_INSTANT_MSG
    if fight is not None and not bool(fight.get("instant")):
        return False, CHALLENGE_ONLY_INSTANT_MSG
    # Legacy V1 (pre-item-12): no fight linkage.
    if fight_id is None and kind is None:
        return True, None
    return False, CHALLENGE_ONLY_INSTANT_MSG


def finalize_instant_ruling(
    db: CourtDB,
    fight_id: int,
    verdict: dict[str, Any],
    *,
    message_id: int | None,
    channel_id: int | None = None,
    guild_id: int | None = None,
    now: datetime | str | None = None,
    parent_ruling_id: int | None = None,
    franchise: str | None = None,
) -> dict[str, Any]:
    """Stamp ``kind='instant'`` ruling and move fight ``proposed`` → ``ruled``.

    No thread is created or archived (``thread_id`` stays null). Instant fights
    are excluded from derived records (item 9).
    """
    from bot.fights import utc_now

    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError("Fight not found.")
    if not bool(fight.get("instant")):
        raise ValueError("finalize_instant_ruling requires an instant fight.")

    v = dict(verdict)
    side_a = str(fight.get("side_a") or "")
    side_b = str(fight.get("side_b") or "")
    matchup = f"{side_a or '?'} vs {side_b or '?'}"
    v.setdefault("matchup", matchup)

    winner_side = resolve_instant_winner_side(v, side_a, side_b)
    if winner_side in {"a", "b"}:
        v["winner_side"] = winner_side
        v.setdefault("winner", side_a if winner_side == "a" else side_b)

    winner_adv = None
    if winner_side == "a" and fight.get("advocate_a_id") is not None:
        winner_adv = int(fight["advocate_a_id"])
    elif winner_side == "b" and fight.get("advocate_b_id") is not None:
        winner_adv = int(fight["advocate_b_id"])

    now_dt = as_datetime(now) if now is not None else utc_now()
    ruled_at = to_iso(now_dt)

    ret_status = v.get("retrieval_status")
    if not isinstance(ret_status, str):
        ret_status = None
    fran = franchise
    if fran is None:
        fran = v.get("franchise") if isinstance(v.get("franchise"), str) else None

    usage = v.get("_usage") if isinstance(v.get("_usage"), dict) else {}
    model = str(usage.get("model") or ruling_model())

    ch = channel_id if channel_id is not None else fight.get("channel_id")
    gid = guild_id if guild_id is not None else fight.get("guild_id")

    ruling_id = db.insert_ruling(
        message_id=message_id,
        channel_id=ch,
        guild_id=gid,
        fighter_a=side_a,
        fighter_b=side_b,
        context=fight.get("context"),
        verdict=v,
        parent_ruling_id=parent_ruling_id,
        franchise=fran,
        retrieval_status=ret_status,
        voided=bool(v.get("voided")),
        fight_id=int(fight_id),
        kind="instant",
        judge_model=model,
        winner_side=winner_side if winner_side in {"a", "b"} else None,
        winner_advocate_id=winner_adv,
        confidence=float(v["confidence"]) if v.get("confidence") is not None else None,
        opening_score_a=v.get("opening_score_a"),
        opening_score_b=v.get("opening_score_b"),
        close_score_a=v.get("close_score_a"),
        close_score_b=v.get("close_score_b"),
        argument_quality=v.get("argument_quality"),
        transcript_snapshot=None,
    )

    for c in v.get("citations") or []:
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

    # proposed → ruled; never create a thread.
    db.update_fight(
        fight_id,
        status=RULED_STATUS,
        ruled_at=ruled_at,
        thread_id=None,
    )
    fight = db.get_fight(fight_id) or fight

    try:
        from bot.budget import current_month, estimate_usd, usage_from_verdict

        u = usage_from_verdict(v)
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
            fight_id=int(fight_id),
            role="ruling",
        )
    except Exception:
        pass

    return {
        "fight": fight,
        "ruling_id": ruling_id,
        "verdict": v,
        "kind": "instant",
        "parent_ruling_id": parent_ruling_id,
    }
