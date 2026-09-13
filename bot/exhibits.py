"""V2 item 7 — exhibit extraction, verification, and ❌ contest.

Amendment 10 / C4: extract from advocate messages only — never create gallery
rows. Amendment 8 / C1: links on allowlist domains are the real verification;
quotes get best-effort normalized substring hits against fight receipts.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Sequence

from bot.db import CourtDB
from bot.sources import load_sources_config, url_allowed
from bot.transcript import MessageLike, filter_advocate_messages

# Straight and curly double quotes.
_QUOTE_RE = re.compile(
    r'[""\u201c\u201d]([^""\u201c\u201d]+)[""\u201c\u201d]'
)
_URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+", re.IGNORECASE)

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

FetchUrl = Callable[[str], Any]  # True / url-str → verified; False/None → fail


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _word_count(text: str) -> int:
    return len([w for w in (text or "").split() if w])


def extract_urls(content: str) -> list[str]:
    found: list[str] = []
    for m in _URL_RE.finditer(content or ""):
        url = m.group(0).rstrip(".,;:!?")
        if url and url not in found:
            found.append(url)
    return found


def extract_quotes(content: str) -> list[str]:
    """Quoted spans with ≥ 8 words (straight or curly double quotes)."""
    out: list[str] = []
    for m in _QUOTE_RE.finditer(content or ""):
        inner = m.group(1).strip()
        if _word_count(inner) >= 8:
            out.append(inner)
    return out


def is_image_attachment(att: Mapping[str, Any] | Any) -> bool:
    if att is None:
        return False
    if not isinstance(att, Mapping):
        # duck-type discord.Attachment
        ct = str(getattr(att, "content_type", None) or "").lower()
        fn = str(getattr(att, "filename", None) or getattr(att, "url", None) or "").lower()
        if ct.startswith("image/"):
            return True
        return any(fn.endswith(ext) for ext in _IMAGE_EXTS)
    ct = str(att.get("content_type") or "").lower()
    if ct.startswith("image/"):
        return True
    fn = str(att.get("filename") or att.get("url") or "").lower()
    return any(fn.endswith(ext) for ext in _IMAGE_EXTS)


def attachment_url(att: Mapping[str, Any] | Any) -> str | None:
    if isinstance(att, Mapping):
        url = att.get("url")
        return str(url) if url else None
    url = getattr(att, "url", None)
    return str(url) if url else None


def default_fetch_url(url: str, *, timeout: float = 5.0) -> str | None:
    """Best-effort GET; returns final URL on HTTP success, else None."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "FightClubBot/2.0 (+exhibit-verify)"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = getattr(resp, "geturl", lambda: url)()
            code = getattr(resp, "status", None) or resp.getcode()
            if code and int(code) >= 400:
                return None
            return str(final or url)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None


def franchise_keys_for_fight(fight: Mapping[str, Any]) -> list[str]:
    keys: list[str] = []
    for k in (fight.get("franchise_a"), fight.get("franchise_b")):
        if k and str(k).strip() and str(k) not in keys:
            keys.append(str(k).strip())
    return keys


def link_on_allowlist(url: str, franchise_keys: Sequence[str]) -> bool:
    if not url:
        return False
    for fk in franchise_keys:
        if url_allowed(url, fk):
            return True
    # Also accept any franchise domain in sources.json when fight has no keys
    # but URL is clearly on an allowlisted host for some franchise.
    if not franchise_keys:
        cfg = load_sources_config()
        for fk in (cfg.get("franchises") or {}):
            if url_allowed(url, fk, cfg):
                return True
    return False


def verify_link(
    url: str,
    franchise_keys: Sequence[str],
    *,
    fetch_url: FetchUrl | None = None,
) -> tuple[str, str | None]:
    """Return (status, verified_url). Allowlisted + fetchable → verified."""
    if not link_on_allowlist(url, franchise_keys):
        return "unverified", None
    fetcher = fetch_url or default_fetch_url
    try:
        result = fetcher(url)
    except Exception:
        return "unverified", None
    if result is False or result is None:
        return "unverified", None
    if isinstance(result, str) and result.strip():
        return "verified", result.strip()
    return "verified", url


def quote_verified_against_receipts(
    quote: str,
    receipts: Sequence[Mapping[str, Any]],
) -> bool:
    """Normalized substring ≥8 words against already-fetched fight receipts."""
    nq = normalize_ws(quote)
    if _word_count(nq) < 8:
        return False
    for r in receipts:
        parts = [
            str(r.get("snippet") or ""),
            str(r.get("claim") or ""),
            str(r.get("text") or ""),
        ]
        hay = normalize_ws(" ".join(parts))
        if nq and nq in hay:
            return True
    return False


def _candidate_exhibits_from_message(
    turn: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Raw exhibit dicts (no status yet) for one advocate turn."""
    content = str(turn.get("content") or "")
    author_id = turn.get("author_id")
    message_id = turn.get("id")
    side = turn.get("side")
    source_role = "advocate"
    out: list[dict[str, Any]] = []

    for url in extract_urls(content):
        out.append(
            {
                "source_role": source_role,
                "kind": "link",
                "text": None,
                "url": url,
                "message_id": message_id,
                "author_id": author_id,
                "side": side,
            }
        )
    for quote in extract_quotes(content):
        out.append(
            {
                "source_role": source_role,
                "kind": "quote",
                "text": quote,
                "url": None,
                "message_id": message_id,
                "author_id": author_id,
                "side": side,
            }
        )
    for att in turn.get("attachments") or []:
        if is_image_attachment(att):
            out.append(
                {
                    "source_role": source_role,
                    "kind": "image",
                    "text": None,
                    "url": attachment_url(att),
                    "message_id": message_id,
                    "author_id": author_id,
                    "side": side,
                }
            )
    return out


def verify_exhibit(
    cand: Mapping[str, Any],
    *,
    franchise_keys: Sequence[str],
    receipts: Sequence[Mapping[str, Any]],
    fetch_url: FetchUrl | None = None,
) -> tuple[str, str | None]:
    kind = cand.get("kind")
    if kind == "image":
        return "unverified", None
    if kind == "link":
        return verify_link(str(cand.get("url") or ""), franchise_keys, fetch_url=fetch_url)
    if kind == "quote":
        text = str(cand.get("text") or "")
        if quote_verified_against_receipts(text, receipts):
            return "verified", None
        return "unverified", None
    return "unverified", None


def extract_exhibits_for_fight(
    db: CourtDB,
    fight: Mapping[str, Any] | int,
    messages: Sequence[MessageLike],
    *,
    fetch_url: FetchUrl | None = None,
    replace: bool = True,
) -> list[dict[str, Any]]:
    """Extract + verify advocate exhibits; insert rows. Never creates gallery rows.

    ``replace=True`` clears prior exhibits for the fight (idempotent re-run at
    judge_ready). Returns the inserted exhibit rows.
    """
    if isinstance(fight, int):
        row = db.get_fight(fight)
        if row is None:
            raise ValueError(f"Fight {fight} not found.")
        fight_row = row
    else:
        fight_row = dict(fight)
        if fight_row.get("id") is not None:
            fresh = db.get_fight(int(fight_row["id"]))
            if fresh is not None:
                fight_row = fresh

    fight_id = int(fight_row["id"])
    if replace:
        db.clear_fight_exhibits(fight_id)

    turns = filter_advocate_messages(fight_row, messages)
    receipts = db.list_receipts(fight_id)
    keys = franchise_keys_for_fight(fight_row)

    inserted_ids: list[int] = []
    for turn in turns:
        for cand in _candidate_exhibits_from_message(turn):
            # Hard rule: never gallery.
            if cand.get("source_role") != "advocate":
                continue
            status, verified_url = verify_exhibit(
                cand,
                franchise_keys=keys,
                receipts=receipts,
                fetch_url=fetch_url,
            )
            eid = db.insert_exhibit(
                fight_id=fight_id,
                source_role="advocate",
                kind=str(cand["kind"]),
                status=status,
                message_id=cand.get("message_id"),
                author_id=cand.get("author_id"),
                text=cand.get("text"),
                url=cand.get("url"),
                verified_url=verified_url,
            )
            inserted_ids.append(eid)

    rows = db.list_exhibits(fight_id)
    return rows


def contest_exhibits_on_message(
    db: CourtDB,
    fight: Mapping[str, Any] | int,
    *,
    message_id: int,
    reactor_id: int,
) -> list[dict[str, Any]]:
    """Opposing advocate ❌ on an advocate message → mark exhibits contested.

    Contested overrides verified/unverified. Returns updated exhibit rows for
    that message (empty if reactor is not the opposing advocate, or no rows).
    """
    if isinstance(fight, int):
        row = db.get_fight(fight)
        if row is None:
            raise ValueError(f"Fight {fight} not found.")
        fight_row = row
    else:
        fight_row = dict(fight)

    a_id = fight_row.get("advocate_a_id")
    b_id = fight_row.get("advocate_b_id")
    try:
        a_i = int(a_id) if a_id is not None else None
        b_i = int(b_id) if b_id is not None else None
        reactor = int(reactor_id)
        mid = int(message_id)
    except (TypeError, ValueError) as e:
        raise ValueError("Invalid fight/message/reactor id") from e

    if reactor not in {a_i, b_i}:
        return []

    exhibits = [
        e for e in db.list_exhibits(int(fight_row["id"])) if e.get("message_id") == mid
    ]
    if not exhibits:
        return []

    # Message author must be the other advocate.
    authors = {e.get("author_id") for e in exhibits if e.get("author_id") is not None}
    if not authors:
        return []
    # Contester must oppose every author on the message (normally one author).
    for author in authors:
        try:
            author_i = int(author)
        except (TypeError, ValueError):
            return []
        if author_i == reactor:
            return []  # cannot contest own exhibits
        if author_i not in {a_i, b_i}:
            return []
        # reactor must be the other advocate
        if {author_i, reactor} != {a_i, b_i}:
            return []

    db.update_exhibits_status_for_message(
        int(fight_row["id"]), mid, status="contested"
    )
    return [
        e for e in db.list_exhibits(int(fight_row["id"])) if e.get("message_id") == mid
    ]


def format_exhibit_ledger(exhibits: Sequence[Mapping[str, Any]]) -> str:
    """Human-readable ledger for the referee prompt (item 8 consumes this)."""
    if not exhibits:
        return "(none)"
    lines: list[str] = []
    for e in exhibits:
        eid = e.get("id")
        kind = e.get("kind")
        status = e.get("status") or "unverified"
        if kind == "image":
            lines.append(
                f"E{eid}: image — advocate posted an image. [{status}]"
            )
        elif kind == "link":
            url = e.get("verified_url") or e.get("url") or ""
            lines.append(f"E{eid}: link {url} [{status}]")
        elif kind == "quote":
            text = str(e.get("text") or "")
            snippet = text if len(text) <= 120 else text[:117] + "..."
            lines.append(f'E{eid}: quote "{snippet}" [{status}]')
        else:
            lines.append(f"E{eid}: {kind} [{status}]")
    return "\n".join(lines)


def prepare_judge_materials(
    db: CourtDB,
    fight_id: int,
    messages: Sequence[MessageLike],
    *,
    fetch_url: FetchUrl | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Build + store transcript snapshot and extract exhibits for item 8.

    Returns ``{snapshot, exhibits, exhibit_ledger}``.
    """
    from bot.transcript import build_and_store_transcript_snapshot

    fight = db.get_fight(fight_id)
    if fight is None:
        raise ValueError(f"Fight {fight_id} not found.")
    snapshot = build_and_store_transcript_snapshot(
        db, fight, messages, max_tokens=max_tokens
    )
    exhibits = extract_exhibits_for_fight(
        db, fight, messages, fetch_url=fetch_url, replace=True
    )
    return {
        "snapshot": snapshot,
        "exhibits": exhibits,
        "exhibit_ledger": format_exhibit_ledger(exhibits),
    }
