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


def _page_fullurl(base: str, title: str, page: dict[str, Any] | None = None) -> str:
    if page:
        for key in ("fullurl", "canonicalurl"):
            val = page.get(key)
            if val:
                return str(val)
    return f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"


def _mediawiki_extract_text(api: str, title: str) -> tuple[str, dict[str, Any]]:
    """Prefer TextExtracts; Fandom often disables it, so fall back to parse+strip."""
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
    page_raw = _http_get(api + "?" + urllib.parse.urlencode(page_params))
    page_data = json.loads(page_raw.decode("utf-8", errors="replace"))
    pages = ((page_data.get("query") or {}).get("pages")) or {}
    page: dict[str, Any] = {}
    extract = ""
    for candidate in pages.values():
        page = candidate
        extract = str(candidate.get("extract") or "").strip()
        if extract:
            return extract, page

    parse_params = {
        "action": "parse",
        "page": title,
        "prop": "text",
        "section": "0",
        "disablelimitreport": "1",
        "format": "json",
    }
    try:
        parse_raw = _http_get(api + "?" + urllib.parse.urlencode(parse_params))
        parse_data = json.loads(parse_raw.decode("utf-8", errors="replace"))
        html = ((parse_data.get("parse") or {}).get("text") or {})
        if isinstance(html, dict):
            html = html.get("*") or ""
        extract = _strip_html(str(html))
        extract = re.sub(r"\s+", " ", extract).strip()
    except Exception:
        extract = ""
    return extract, page


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
        extract, page = _mediawiki_extract_text(api, title)
        if not extract or len(extract.split()) < 8:
            continue
        fullurl = _page_fullurl(base, title, page)
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
