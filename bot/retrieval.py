"""Allowlisted wiki retrieval via stdlib urllib (no extra deps)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

from bot.sources import (
    cache_ttl_seconds,
    cap_snippet,
    detect_franchise,
    franchise_sources,
    is_hard_no_url,
    load_sources_config,
    url_allowed,
)

USER_AGENT = "FightClubCourt/0.3 (+https://github.com/mcsanchez27/fight-club; receipts)"
FETCH_TIMEOUT = 8


@dataclass
class Passage:
    claim: str
    source_url: str
    locator: str
    snippet: str
    verified: bool
    retrieved_at: str
    kind: str = "receipt"  # receipt | exhibit
    source_title: str = ""
    retrieval_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Simple in-process cache: key -> (expires_at, Passage list)
_CACHE: dict[str, tuple[float, list[Passage]]] = {}


def retrieval_enabled() -> bool:
    return os.getenv("FIGHT_RETRIEVAL_ENABLED", "1").strip() not in {"0", "false", "False", "no"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        t = data.strip()
        if t:
            self._chunks.append(t)

    def text(self) -> str:
        return " ".join(self._chunks)


def _http_get(url: str) -> bytes:
    if is_hard_no_url(url):
        raise ValueError(f"hard-no URL blocked: {url}")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def _strip_html(html: str) -> str:
    p = _HTMLTextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    return p.text()


def _mediawiki_search_and_extract(
    base_url: str, query: str, source_name: str, franchise_key: str
) -> list[Passage]:
    base = base_url.rstrip("/")
    api = f"{base}/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": "2",
        "format": "json",
    }
    search_url = api + "?" + urllib.parse.urlencode(params)
    raw = _http_get(search_url)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    hits = (((data.get("query") or {}).get("search")) or [])[:2]
    out: list[Passage] = []
    now = _now_iso()
    for hit in hits:
        title = str(hit.get("title") or query)
        page_params = {
            "action": "query",
            "prop": "extracts|info",
            "exintro": "1",
            "explaintext": "1",
            "exchars": "600",
            "titles": title,
            "inprop": "url",
            "format": "json",
        }
        page_url = api + "?" + urllib.parse.urlencode(page_params)
        page_raw = _http_get(page_url)
        page_data = json.loads(page_raw.decode("utf-8", errors="replace"))
        pages = ((page_data.get("query") or {}).get("pages")) or {}
        for page in pages.values():
            extract = str(page.get("extract") or "").strip()
            fullurl = str(page.get("fullurl") or f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}")
            if not extract:
                continue
            if not url_allowed(fullurl, franchise_key):
                continue
            out.append(
                Passage(
                    claim=f"{title} (wiki extract)",
                    source_url=fullurl,
                    locator=f"{source_name} · {title}",
                    snippet=cap_snippet(extract),
                    verified=True,
                    retrieved_at=now,
                    kind="receipt",
                    source_title=source_name,
                )
            )
    return out


def _html_search_snippet(
    base_url: str, query: str, source_name: str, franchise_key: str
) -> list[Passage]:
    """Best-effort HTML fetch for non-MediaWiki allowlisted sites (e.g. Kanzenshuu)."""
    base = base_url.rstrip("/")
    # Prefer a site search page if present; otherwise fetch homepage-ish search URL.
    q = urllib.parse.quote_plus(query)
    candidates = [
        f"{base}/?s={q}",
        f"{base}/search?q={q}",
        f"{base}/",
    ]
    now = _now_iso()
    for url in candidates:
        if not url_allowed(url, franchise_key) and url.rstrip("/") != base:
            # homepage of allowlisted base is ok for a locator stub
            if urllib.parse.urlparse(url).hostname != urllib.parse.urlparse(base).hostname:
                continue
        try:
            raw = _http_get(url)
        except Exception:
            continue
        text = _strip_html(raw.decode("utf-8", errors="replace"))
        # Find a window around the query terms
        lower = text.lower()
        needle = query.lower().split()[0] if query.strip() else ""
        idx = lower.find(needle) if needle else -1
        if idx < 0:
            snippet_src = text[:400]
        else:
            start = max(0, idx - 80)
            snippet_src = text[start : start + 400]
        snippet_src = re.sub(r"\s+", " ", snippet_src).strip()
        if len(snippet_src.split()) < 5:
            continue
        return [
            Passage(
                claim=f"{query} ({source_name})",
                source_url=url if url_allowed(url, franchise_key) else base,
                locator=f"{source_name} · search:{query}",
                snippet=cap_snippet(snippet_src),
                verified=True,
                retrieved_at=now,
                kind="receipt",
                source_title=source_name,
            )
        ]
    return []


def _fetch_for_query(franchise_key: str, query: str) -> list[Passage]:
    cfg = load_sources_config()
    passages: list[Passage] = []
    for src in franchise_sources(franchise_key, cfg):
        name = str(src.get("name") or "source")
        base = str(src.get("base_url") or "")
        api = str(src.get("api") or "mediawiki")
        if not base or is_hard_no_url(base, cfg):
            continue
        try:
            if api == "mediawiki":
                passages.extend(
                    _mediawiki_search_and_extract(base, query, name, franchise_key)
                )
            else:
                passages.extend(_html_search_snippet(base, query, name, franchise_key))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
            continue
        except Exception:
            continue
    return passages


def exhibit_from_text(
    text: str,
    *,
    franchise_key: str | None,
    source_url: str | None = None,
) -> Passage:
    """User-pasted text → EXHIBIT. verified only if URL is allowlisted."""
    claim = (text or "").strip()
    url = (source_url or "").strip() or None
    verified = bool(url and franchise_key and url_allowed(url, franchise_key))
    # If the paste itself embeds a hard-no URL, force unverified and strip URL
    if url and is_hard_no_url(url):
        verified = False
        url = None
    return Passage(
        claim=cap_snippet(claim, 40) if claim else "exhibit",
        source_url=url or "",
        locator="user exhibit",
        snippet=cap_snippet(claim),
        verified=verified,
        retrieved_at=_now_iso(),
        kind="exhibit",
        source_title="User exhibit",
    )


@dataclass
class RetrievalResult:
    franchise: str | None
    status: str  # ok | unavailable | unlisted | disabled
    receipts: list[Passage]
    exhibits: list[Passage]

    @property
    def retrieval_unavailable(self) -> bool:
        return self.status in {"unavailable", "unlisted", "disabled"}


def retrieve(
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
    *,
    franchise_hint: str | None = None,
    exhibits: list[str] | None = None,
) -> RetrievalResult:
    """Fetch allowlisted receipts; build exhibits from user pastes.

    Never raises for network failure — returns status=unavailable instead.
    Unlisted franchise → status=unlisted, empty receipts (legal plea path).
    """
    exhibit_passages = [
        exhibit_from_text(t, franchise_key=None) for t in (exhibits or []) if t and t.strip()
    ]

    if not retrieval_enabled():
        # Still attach exhibits (unverified unless URL later); no autonomous fetch
        return RetrievalResult(
            franchise=detect_franchise(fighter_a, fighter_b, context, franchise_hint),
            status="disabled",
            receipts=[],
            exhibits=exhibit_passages,
        )

    franchise = detect_franchise(fighter_a, fighter_b, context, franchise_hint)
    if not franchise:
        # Re-tag exhibits without franchise (cannot verify URLs)
        return RetrievalResult(
            franchise=None,
            status="unlisted",
            receipts=[],
            exhibits=[
                exhibit_from_text(t, franchise_key=None)
                for t in (exhibits or [])
                if t and t.strip()
            ],
        )

    # Re-build exhibits with franchise for URL verification
    exhibit_passages = [
        exhibit_from_text(t, franchise_key=franchise)
        for t in (exhibits or [])
        if t and t.strip()
    ]

    queries = [fighter_a.strip(), fighter_b.strip()]
    if context and context.strip():
        queries.append(context.strip()[:80])

    receipts: list[Passage] = []
    ttl = cache_ttl_seconds()
    any_attempted = False
    any_success = False

    for q in queries:
        if not q:
            continue
        cache_key = f"{franchise}::{q.lower()}"
        hit = _CACHE.get(cache_key)
        if hit and hit[0] > time.time():
            receipts.extend(hit[1])
            any_attempted = True
            any_success = True
            continue
        any_attempted = True
        try:
            found = _fetch_for_query(franchise, q)
        except Exception:
            found = []
        if found:
            any_success = True
            _CACHE[cache_key] = (time.time() + ttl, found)
            receipts.extend(found)

    # Dedup by source_url+locator
    seen: set[str] = set()
    deduped: list[Passage] = []
    for i, p in enumerate(receipts):
        key = f"{p.source_url}|{p.locator}"
        if key in seen:
            continue
        seen.add(key)
        p.retrieval_id = f"ret_{len(deduped) + 1}"
        deduped.append(p)

    if not any_success:
        status = "unavailable" if any_attempted else "unavailable"
        return RetrievalResult(
            franchise=franchise,
            status=status,
            receipts=[],
            exhibits=exhibit_passages,
        )

    return RetrievalResult(
        franchise=franchise,
        status="ok",
        receipts=deduped[:8],
        exhibits=exhibit_passages,
    )


def clear_retrieval_cache() -> None:
    _CACHE.clear()


def pack_retrieval_for_prompt(result: RetrievalResult) -> str:
    """Format receipts/exhibits for the judge user message."""
    lines: list[str] = []
    lines.append(f"RETRIEVAL_STATUS: {result.status}")
    if result.franchise:
        lines.append(f"FRANCHISE: {result.franchise}")
    if result.retrieval_unavailable:
        lines.append(
            "Retrieval unavailable or franchise unlisted. Put missing canon in unknowns "
            "(legal plea). Do NOT invent URLs. Confidence will be capped at 5/10."
        )
    if result.receipts:
        lines.append("RECEIPTS (autonomous fetch — cite these for load-bearing ruling/concessions):")
        for p in result.receipts:
            lines.append(
                f"- [{p.retrieval_id}] {p.locator} | {p.source_url}\n"
                f"  snippet: {p.snippet}"
            )
    if result.exhibits:
        lines.append("EXHIBITS (user-pasted — verify against allowlist; flag unverified):")
        for i, p in enumerate(result.exhibits, start=1):
            flag = "verified" if p.verified else "unverified"
            lines.append(
                f"- [ex_{i}] ({flag}) {p.claim}\n"
                f"  snippet: {p.snippet}"
                + (f" | {p.source_url}" if p.source_url else "")
            )
    if not result.receipts and not result.exhibits:
        lines.append("No receipts or exhibits packed.")
    return "\n".join(lines)
