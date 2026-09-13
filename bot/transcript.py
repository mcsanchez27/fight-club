"""V2 item 7 — transcript assembly (amendment 2: snapshot is sole authority).

Live Discord history builds the snapshot once when the fight enters
``judge_ready``. Judge / reconsideration (item 8+) read the stored snapshot
only — never re-fetch Discord as a second source of truth.
"""

from __future__ import annotations

import copy
from typing import Any, Mapping, MutableMapping, Sequence

from bot.budget import estimate_tokens
from bot.db import CourtDB

DEFAULT_TRANSCRIPT_MAX_TOKENS = 20_000

# Message-like: author_id, content, id; attachments optional.
MessageLike = Mapping[str, Any]


def transcript_max_tokens(db: CourtDB | None, guild_id: int | None) -> int:
    """guild_config → env → 20000 (§7)."""
    from bot.config import get_guild_config

    return int(get_guild_config(db, guild_id, "transcript_max_tokens"))


def _msg_id(msg: MessageLike) -> int | None:
    raw = msg.get("id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _author_id(msg: MessageLike) -> int | None:
    raw = msg.get("author_id")
    if raw is None:
        # Discord-ish objects sometimes nest author
        author = msg.get("author")
        if isinstance(author, Mapping):
            raw = author.get("id")
        elif author is not None and hasattr(author, "id"):
            raw = getattr(author, "id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _content(msg: MessageLike) -> str:
    return str(msg.get("content") or "")


def label_for_side(side: str, side_name: str | None) -> str:
    name = (side_name or side.upper()).strip() or side.upper()
    prefix = "A" if side == "a" else "B"
    return f"[{prefix}:{name}]"


def filter_advocate_messages(
    fight: Mapping[str, Any],
    messages: Sequence[MessageLike],
) -> list[dict[str, Any]]:
    """Keep advocate messages only; attach side + label. Chronological order preserved."""
    a_id = fight.get("advocate_a_id")
    b_id = fight.get("advocate_b_id")
    try:
        a_id_i = int(a_id) if a_id is not None else None
    except (TypeError, ValueError):
        a_id_i = None
    try:
        b_id_i = int(b_id) if b_id is not None else None
    except (TypeError, ValueError):
        b_id_i = None

    out: list[dict[str, Any]] = []
    for msg in messages:
        aid = _author_id(msg)
        if aid is None:
            continue
        if a_id_i is not None and aid == a_id_i:
            side = "a"
            label = label_for_side("a", fight.get("side_a"))
        elif b_id_i is not None and aid == b_id_i:
            side = "b"
            label = label_for_side("b", fight.get("side_b"))
        else:
            continue
        attachments = msg.get("attachments")
        if attachments is None:
            attachments = []
        else:
            attachments = list(attachments)
        out.append(
            {
                "id": _msg_id(msg),
                "author_id": aid,
                "content": _content(msg),
                "attachments": copy.deepcopy(attachments),
                "side": side,
                "label": label,
            }
        )
    return out


def _line_text(turn: Mapping[str, Any]) -> str:
    body = str(turn.get("content") or "")
    return f"{turn['label']} {body}".rstrip()


def _transcript_token_count(turns: Sequence[Mapping[str, Any]]) -> int:
    if not turns:
        return 0
    text = "\n".join(_line_text(t) for t in turns)
    return estimate_tokens(text)


def _middle_truncate_sides(
    turns: list[dict[str, Any]],
    max_tokens: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Drop middle turns per side evenly; never openings/closings of a side."""
    if max_tokens <= 0:
        return turns, False
    if _transcript_token_count(turns) <= max_tokens:
        return turns, False

    # Working lists of indices into ``turns`` for each side.
    a_idx = [i for i, t in enumerate(turns) if t["side"] == "a"]
    b_idx = [i for i, t in enumerate(turns) if t["side"] == "b"]
    drop: set[int] = set()

    def remaining(side_idxs: list[int]) -> list[int]:
        return [i for i in side_idxs if i not in drop]

    truncated = False
    # Safety bound: at most len(turns) removals.
    for _ in range(len(turns) + 1):
        if _transcript_token_count(
            [t for i, t in enumerate(turns) if i not in drop]
        ) <= max_tokens:
            break
        rem_a = remaining(a_idx)
        rem_b = remaining(b_idx)
        # Removable = middle only (keep first + last when len >= 2).
        can_a = max(0, len(rem_a) - 2)
        can_b = max(0, len(rem_b) - 2)
        if can_a == 0 and can_b == 0:
            break
        # Evenly: prefer the side with more removable middles.
        if can_a >= can_b and can_a > 0:
            mid = len(rem_a) // 2
            # ensure not first/last
            if mid <= 0:
                mid = 1
            if mid >= len(rem_a) - 1:
                mid = len(rem_a) - 2
            drop.add(rem_a[mid])
            truncated = True
        elif can_b > 0:
            mid = len(rem_b) // 2
            if mid <= 0:
                mid = 1
            if mid >= len(rem_b) - 1:
                mid = len(rem_b) - 2
            drop.add(rem_b[mid])
            truncated = True
        else:
            break

    kept = [t for i, t in enumerate(turns) if i not in drop]
    return kept, truncated


def assemble_transcript(
    fight: Mapping[str, Any],
    messages: Sequence[MessageLike],
    *,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Filter, label, middle-truncate; return a freeze-ready snapshot dict.

    Snapshot shape (JSON-serializable)::

        {
          "version": 1,
          "lines": ["[A:Goku] …", …],
          "turns": [{id, author_id, side, label, content, attachments}, …],
          "text": "…\\n…",
          "truncated": bool,
          "max_tokens": int,
        }
    """
    cap = (
        int(max_tokens)
        if max_tokens is not None
        else DEFAULT_TRANSCRIPT_MAX_TOKENS
    )
    filtered = filter_advocate_messages(fight, messages)
    # Deep-copy so later mutations to input/attachments cannot affect snapshot.
    turns = copy.deepcopy(filtered)
    turns, truncated = _middle_truncate_sides(turns, cap)
    lines = [_line_text(t) for t in turns]
    text = "\n".join(lines)
    # Strip bulky attachment payloads from stored turns (keep metadata only).
    slim_turns: list[dict[str, Any]] = []
    for t in turns:
        atts = []
        for att in t.get("attachments") or []:
            if isinstance(att, Mapping):
                atts.append(
                    {
                        k: att.get(k)
                        for k in ("id", "url", "filename", "content_type")
                        if att.get(k) is not None
                    }
                )
            else:
                atts.append(att)
        slim_turns.append(
            {
                "id": t.get("id"),
                "author_id": t.get("author_id"),
                "side": t.get("side"),
                "label": t.get("label"),
                "content": t.get("content"),
                "attachments": atts,
            }
        )
    return {
        "version": 1,
        "lines": lines,
        "turns": slim_turns,
        "text": text,
        "truncated": truncated,
        "max_tokens": cap,
    }


def build_and_store_transcript_snapshot(
    db: CourtDB,
    fight: Mapping[str, Any] | int,
    messages: Sequence[MessageLike],
    *,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Assemble snapshot and store on ``fights.transcript_snapshot`` (staging for item 8).

    Returns the frozen snapshot dict. Input ``messages`` may be mutated afterward
    without affecting the stored snapshot.
    """
    if isinstance(fight, int):
        row = db.get_fight(fight)
        if row is None:
            raise ValueError(f"Fight {fight} not found.")
        fight_row: dict[str, Any] = row
    else:
        fight_row = dict(fight)
        fid = fight_row.get("id")
        if fid is not None:
            fresh = db.get_fight(int(fid))
            if fresh is not None:
                fight_row = fresh

    fight_id = int(fight_row["id"])
    guild_id = fight_row.get("guild_id")
    try:
        guild_i = int(guild_id) if guild_id is not None else None
    except (TypeError, ValueError):
        guild_i = None

    cap = max_tokens
    if cap is None:
        cap = transcript_max_tokens(db, guild_i)

    # Snapshot from a deep-copied message list so callers cannot poison storage.
    msgs_copy = copy.deepcopy(list(messages))
    snapshot = assemble_transcript(fight_row, msgs_copy, max_tokens=cap)
    # Store another deep copy so in-memory return value is independent too.
    stored = copy.deepcopy(snapshot)
    db.update_fight(fight_id, transcript_snapshot=stored)
    return copy.deepcopy(snapshot)


def snapshot_text(snapshot: Mapping[str, Any] | str | None) -> str:
    """Plain transcript text for the referee prompt."""
    if snapshot is None:
        return ""
    if isinstance(snapshot, str):
        return snapshot
    text = snapshot.get("text")
    if isinstance(text, str):
        return text
    lines = snapshot.get("lines")
    if isinstance(lines, list):
        return "\n".join(str(x) for x in lines)
    return ""
