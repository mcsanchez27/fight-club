"""Allowlisted wiki retrieval via stdlib urllib (no extra deps)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
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
# Global wall-clock budget for all wiki/HTML fetches in one retrieve() call (Tech Design §9).
RETRIEVAL_BUDGET_SECONDS = float(os.getenv("FIGHT_RETRIEVAL_BUDGET_SECONDS", "10"))


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


def _remaining_timeout(deadline: float | None, default: float = FETCH_TIMEOUT) -> float:
    """Seconds left before deadline, capped by default per-request timeout."""
    if deadline is None:
        return default
    left = deadline - time.monotonic()
    if left <= 0:
        return 0.0
    return min(default, left)


def _http_get(url: str, *, timeout: float | None = None, deadline: float | None = None) -> bytes:
    if is_hard_no_url(url):
        raise ValueError(f"hard-no URL blocked: {url}")
    if timeout is None:
        timeout = _remaining_timeout(deadline, FETCH_TIMEOUT)
    if timeout <= 0:
        raise TimeoutError("retrieval budget exhausted")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json,text/html,*/*"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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



def _normalize_title_text(s: str) -> str:
    s = (s or "").replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _core_title(s: str) -> str:
    """Title core for matching: strip parenthetical disambiguation; keep slash paths intact."""
    s = _normalize_title_text(s)
    s = re.sub(r"\s*\([^)]*\)\s*", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_matches_query(title: str, query: str) -> bool:
    """Strict match: after stripping parentheticals, core title must equal query (casefold).

    Accepts "Goku", "Goku (Dragon Ball)". Rejects weak hits like "King Vegeta",
    "Vegeta Saga", "Goku/Dragonball Evolution", "Den-Goku".
    """
    q = _core_title(query).casefold()
    t = _core_title(title).casefold()
    if not q or not t:
        return False
    return t == q


def _page_is_missing(page: dict[str, Any] | None) -> bool:
    if not page:
        return True
    if "missing" in page:
        return True
    pid = page.get("pageid")
    if pid == -1 or pid == "-1":
        return True
    return False


def _is_site_homepage(url: str, base: str) -> bool:
    return (url or "").rstrip("/") == (base or "").rstrip("/")


def _html_url_is_verified(url: str, base: str) -> bool:
    """Verified HTML receipts: not a search URL, not the bare site homepage, path deeper than /."""
    if not url:
        return False
    lower = url.lower()
    if any(tok in lower for tok in ("?s=", "/search", "search?")):
        return False
    if _is_site_homepage(url, base):
        return False
    path = urllib.parse.urlparse(url).path or "/"
    if path.rstrip("/") == "":
        return False
    return True


def _mediawiki_extract_text(
    api: str, title: str, *, deadline: float | None = None
) -> tuple[str, dict[str, Any]]:
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
    page_raw = _http_get(
        api + "?" + urllib.parse.urlencode(page_params), deadline=deadline
    )
    page_data = json.loads(page_raw.decode("utf-8", errors="replace"))
    pages = ((page_data.get("query") or {}).get("pages")) or {}
    page: dict[str, Any] = {}
    extract = ""
    for candidate in pages.values():
        page = candidate
        if _page_is_missing(page):
            return "", page
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
        parse_raw = _http_get(
            api + "?" + urllib.parse.urlencode(parse_params), deadline=deadline
        )
        parse_data = json.loads(parse_raw.decode("utf-8", errors="replace"))
        html = ((parse_data.get("parse") or {}).get("text") or {})
        if isinstance(html, dict):
            html = html.get("*") or ""
        extract = _strip_html(str(html))
        extract = re.sub(r"\s+", " ", extract).strip()
    except Exception:
        extract = ""
    return extract, page


def _mediawiki_passage_from_title(
    api: str,
    base: str,
    title: str,
    query: str,
    source_name: str,
    franchise_key: str,
    now: str,
    *,
    deadline: float | None = None,
) -> Passage | None:
    extract, page = _mediawiki_extract_text(api, title, deadline=deadline)
    if _page_is_missing(page):
        return None
    actual_title = str(page.get("title") or title)
    if not _title_matches_query(actual_title, query):
        return None
    if not extract or len(extract.split()) < 8:
        return None
    fullurl = _page_fullurl(base, actual_title, page)
    if not url_allowed(fullurl, franchise_key):
        return None
    return Passage(
        claim=f"{actual_title} (wiki extract)",
        source_url=fullurl,
        locator=f"{source_name} · {actual_title}",
        snippet=cap_snippet(extract),
        verified=True,
        retrieved_at=now,
        kind="receipt",
        source_title=source_name,
    )


def _mediawiki_search_and_extract(
    base_url: str,
    query: str,
    source_name: str,
    franchise_key: str,
    *,
    deadline: float | None = None,
) -> list[Passage]:
    """Exact-title lookup first (titles=), then filtered search; reject weak title matches."""
    if deadline is not None and time.monotonic() >= deadline:
        return []
    base = base_url.rstrip("/")
    api = f"{base}/api.php"
    now = _now_iso()
    q = (query or "").strip()
    if not q:
        return []

    title_candidates = [q]
    titled = q.title()
    if titled.casefold() != q.casefold():
        title_candidates.append(titled)

    for title_try in title_candidates:
        if deadline is not None and time.monotonic() >= deadline:
            return []
        hit = _mediawiki_passage_from_title(
            api, base, title_try, q, source_name, franchise_key, now, deadline=deadline
        )
        if hit:
            return [hit]

    params = {
        "action": "query",
        "list": "search",
        "srsearch": q,
        "srlimit": "5",
        "format": "json",
    }
    raw = _http_get(api + "?" + urllib.parse.urlencode(params), deadline=deadline)
    data = json.loads(raw.decode("utf-8", errors="replace"))
    hits = (((data.get("query") or {}).get("search")) or [])[:5]
    out: list[Passage] = []
    for hit in hits:
        if deadline is not None and time.monotonic() >= deadline:
            break
        title = str(hit.get("title") or "")
        if not title or not _title_matches_query(title, q):
            continue
        passage = _mediawiki_passage_from_title(
            api, base, title, q, source_name, franchise_key, now, deadline=deadline
        )
        if passage:
            out.append(passage)
        if len(out) >= 2:
            break
    return out


def _html_search_snippet(
    base_url: str,
    query: str,
    source_name: str,
    franchise_key: str,
    *,
    deadline: float | None = None,
) -> list[Passage]:
    """Best-effort HTML fetch for non-MediaWiki allowlisted sites (e.g. Kanzenshuu).

    Never treats the bare homepage as a verified receipt; homepage is not a candidate.
    """
    if deadline is not None and time.monotonic() >= deadline:
        return []
    base = base_url.rstrip("/")
    q = urllib.parse.quote_plus(query)
    # Do not include bare homepage — it must never count as a verified receipt.
    candidates = [f"{base}/?s={q}", f"{base}/search?q={q}"]
    now = _now_iso()
    for url in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not url_allowed(url, franchise_key) and url.rstrip("/") != base:
            if urllib.parse.urlparse(url).hostname != urllib.parse.urlparse(base).hostname:
                continue
        try:
            raw = _http_get(url, deadline=deadline)
        except Exception:
            continue
        body = _strip_html(raw.decode("utf-8", errors="replace"))
        lower = body.lower()
        needle = query.lower().split()[0] if query.strip() else ""
        idx = lower.find(needle) if needle else -1
        snippet_src = body[:400] if idx < 0 else body[max(0, idx - 80) : max(0, idx - 80) + 400]
        snippet_src = re.sub(r"\s+", " ", snippet_src).strip()
        if not _looks_like_article_snippet(snippet_src, query):
            continue
        article_url = url if url_allowed(url, franchise_key) else base
        verified = _html_url_is_verified(article_url, base)
        return [
            Passage(
                claim=f"{query} ({source_name})",
                source_url=article_url,
                locator=f"{source_name} · search:{query}",
                snippet=cap_snippet(snippet_src),
                verified=verified,
                retrieved_at=now,
                kind="receipt",
                source_title=source_name,
            )
        ]
    return []


_SEARCH_CHROME = re.compile(
    r"you searched for|search results|forum wiki news|general info faqs|press archive|newbie guide|\bno results\b",
    re.IGNORECASE,
)


def _looks_like_article_snippet(text: str, query: str) -> bool:
    """Reject nav/search chrome so junk HTML does not count as a verified receipt."""
    words = (text or "").split()
    if len(words) < 12:
        return False
    if _SEARCH_CHROME.search(text or ""):
        return False
    needle = (query or "").lower().split()[0] if query else ""
    if needle and needle not in text.lower():
        return False
    return True


def _fetch_one_source(
    franchise_key: str,
    query: str,
    src: dict[str, Any],
    *,
    deadline: float | None = None,
    cfg: dict[str, Any] | None = None,
) -> list[Passage]:
    """Fetch passages for a single allowlisted source (one independent unit of work)."""
    if deadline is not None and time.monotonic() >= deadline:
        return []
    cfg = cfg or load_sources_config()
    name = str(src.get("name") or "source")
    base = str(src.get("base_url") or "")
    api = str(src.get("api") or "mediawiki")
    if not base or is_hard_no_url(base, cfg):
        return []
    try:
        if api == "mediawiki":
            return _mediawiki_search_and_extract(
                base, query, name, franchise_key, deadline=deadline
            )
        return _html_search_snippet(
            base, query, name, franchise_key, deadline=deadline
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError):
        return []
    except Exception:
        return []


def _fetch_for_query(
    franchise_key: str, query: str, *, deadline: float | None = None
) -> list[Passage]:
    """Sequential multi-source fetch (kept for tests / single-query callers)."""
    cfg = load_sources_config()
    passages: list[Passage] = []
    for src in franchise_sources(franchise_key, cfg):
        if deadline is not None and time.monotonic() >= deadline:
            break
        passages.extend(
            _fetch_one_source(franchise_key, query, src, deadline=deadline, cfg=cfg)
        )
    return passages


def exhibit_from_text(text: str, *, franchise_key: str | None, source_url: str | None = None) -> Passage:
    """User-pasted text → EXHIBIT. verified only if URL is allowlisted."""
    claim = (text or "").strip()
    url = (source_url or "").strip() or None
    verified = bool(url and franchise_key and url_allowed(url, franchise_key))
    if url and is_hard_no_url(url):
        verified = False
        url = None
    return Passage(claim=cap_snippet(claim, 40) if claim else "exhibit", source_url=url or "", locator="user exhibit", snippet=cap_snippet(claim), verified=verified, retrieved_at=_now_iso(), kind="exhibit", source_title="User exhibit")


@dataclass
class RetrievalResult:
    franchise: str | None
    status: str
    receipts: list[Passage]
    exhibits: list[Passage]
    retrieval_seconds: float = 0.0

    @property
    def retrieval_unavailable(self) -> bool:
        return self.status in {"unavailable", "unlisted", "disabled"}


def retrieve(fighter_a: str, fighter_b: str, context: str | None = None, *, franchise_hint: str | None = None, exhibits: list[str] | None = None, budget_seconds: float | None = None) -> RetrievalResult:
    """Fetch allowlisted receipts; build exhibits from user pastes. Never raises for network failure.

    Independent wiki/HTML fetches run in parallel under a hard wall-clock budget
    (default ~10s, Tech Design §9). Round-3 rules unchanged: exact-title first,
    no homepage verified, code-owned verified.
    """
    t0 = time.monotonic()
    budget = RETRIEVAL_BUDGET_SECONDS if budget_seconds is None else float(budget_seconds)
    deadline = t0 + max(0.0, budget)

    def _elapsed() -> float:
        return round(time.monotonic() - t0, 4)

    exhibit_passages = [exhibit_from_text(t, franchise_key=None) for t in (exhibits or []) if t and t.strip()]
    if not retrieval_enabled():
        return RetrievalResult(
            franchise=detect_franchise(fighter_a, fighter_b, context, franchise_hint),
            status="disabled",
            receipts=[],
            exhibits=exhibit_passages,
            retrieval_seconds=_elapsed(),
        )
    franchise = detect_franchise(fighter_a, fighter_b, context, franchise_hint)
    if not franchise:
        return RetrievalResult(
            franchise=None,
            status="unlisted",
            receipts=[],
            exhibits=[exhibit_from_text(t, franchise_key=None) for t in (exhibits or []) if t and t.strip()],
            retrieval_seconds=_elapsed(),
        )
    exhibit_passages = [exhibit_from_text(t, franchise_key=franchise) for t in (exhibits or []) if t and t.strip()]
    queries = [fighter_a.strip(), fighter_b.strip()]
    if context and context.strip():
        queries.append(context.strip()[:80])
    queries = [q for q in queries if q]

    receipts: list[Passage] = []
    ttl = cache_ttl_seconds()
    any_attempted = False
    any_success = False
    cfg = load_sources_config()
    sources = list(franchise_sources(franchise, cfg))

    # Cache hits first (no network); collect miss jobs for parallel fetch.
    jobs: list[tuple[str, dict[str, Any]]] = []
    for q in queries:
        cache_key = f"{franchise}::{q.lower()}"
        hit = _CACHE.get(cache_key)
        if hit and hit[0] > time.time():
            receipts.extend(hit[1])
            any_attempted = True
            any_success = True
            continue
        any_attempted = True
        for src in sources:
            jobs.append((q, src))

    # Parallel independent fetches under the global deadline.
    found_by_query: dict[str, list[Passage]] = {q: [] for q, _ in jobs}
    if jobs and time.monotonic() < deadline:
        max_workers = min(6, len(jobs))
        ex = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_map = {
                ex.submit(
                    _fetch_one_source, franchise, q, src, deadline=deadline, cfg=cfg
                ): q
                for q, src in jobs
            }
            pending = set(future_map.keys())
            while pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                done, pending = wait(pending, timeout=left, return_when=FIRST_COMPLETED)
                for fut in done:
                    q = future_map[fut]
                    try:
                        found = fut.result(timeout=0)
                    except Exception:
                        found = []
                    if found:
                        found_by_query.setdefault(q, []).extend(found)
        finally:
            # Do not block on stragglers past the budget.
            ex.shutdown(wait=False, cancel_futures=True)

    for q, found in found_by_query.items():
        if not found:
            continue
        any_success = True
        # Dedup within query before caching
        seen_q: set[str] = set()
        dedup_q: list[Passage] = []
        for p in found:
            key = f"{p.source_url}|{p.locator}"
            if key in seen_q:
                continue
            seen_q.add(key)
            dedup_q.append(p)
        _CACHE[f"{franchise}::{q.lower()}"] = (time.time() + ttl, dedup_q)
        receipts.extend(dedup_q)

    seen: set[str] = set()
    deduped: list[Passage] = []
    for p in receipts:
        key = f"{p.source_url}|{p.locator}"
        if key in seen:
            continue
        seen.add(key)
        p.retrieval_id = f"ret_{len(deduped) + 1}"
        deduped.append(p)
    elapsed = _elapsed()
    if not any_success:
        # Distinguish "never tried" vs "tried and failed/budget" — same unavailable as V1.
        return RetrievalResult(
            franchise=franchise,
            status="unavailable",
            receipts=[],
            exhibits=exhibit_passages,
            retrieval_seconds=elapsed,
        )
    return RetrievalResult(
        franchise=franchise,
        status="ok",
        receipts=deduped[:8],
        exhibits=exhibit_passages,
        retrieval_seconds=elapsed,
    )


def _merge_retrieval_results(parts: list[RetrievalResult]) -> RetrievalResult:
    """Combine per-side retrieve() results under one accept-time status."""
    if not parts:
        return RetrievalResult(
            franchise=None,
            status="unavailable",
            receipts=[],
            exhibits=[],
            retrieval_seconds=0.0,
        )
    receipts: list[Passage] = []
    exhibits: list[Passage] = []
    franchises: list[str] = []
    seconds = 0.0
    statuses: list[str] = []
    for part in parts:
        receipts.extend(part.receipts)
        exhibits.extend(part.exhibits)
        seconds += float(part.retrieval_seconds or 0.0)
        statuses.append(part.status)
        if part.franchise:
            franchises.append(part.franchise)
    # Re-number retrieval ids after merge.
    seen: set[str] = set()
    deduped: list[Passage] = []
    for p in receipts:
        key = f"{p.source_url}|{p.locator}"
        if key in seen:
            continue
        seen.add(key)
        p.retrieval_id = f"ret_{len(deduped) + 1}"
        deduped.append(p)
    if deduped:
        status = "ok"
    elif "unavailable" in statuses:
        status = "unavailable"
    elif all(s == "disabled" for s in statuses):
        status = "disabled"
    elif all(s in {"unlisted", "disabled"} for s in statuses):
        status = "unlisted"
    else:
        status = statuses[0] if len(set(statuses)) == 1 else "unavailable"
    franchise = franchises[0] if len(set(franchises)) == 1 else (franchises[0] if franchises else None)
    return RetrievalResult(
        franchise=franchise,
        status=status,
        receipts=deduped[:8],
        exhibits=exhibits,
        retrieval_seconds=round(seconds, 4),
    )


def retrieve_for_accept(
    side_a: str,
    side_b: str,
    context: str | None = None,
    *,
    franchise_a: str | None = None,
    franchise_b: str | None = None,
    budget_seconds: float | None = None,
) -> RetrievalResult:
    """Accept-time retrieval using balance franchise keys (Tech Design §5 / C2).

    Best-effort under the global ~10s budget (item 1 parallel retrieve). Never
    raises for network/budget failure — caller always completes Accept.
    """
    budget = RETRIEVAL_BUDGET_SECONDS if budget_seconds is None else float(budget_seconds)
    t0 = time.monotonic()
    deadline = t0 + max(0.0, budget)
    fa = (franchise_a or "").strip() or None
    fb = (franchise_b or "").strip() or None

    def _left() -> float:
        return max(0.0, deadline - time.monotonic())

    if not retrieval_enabled():
        return RetrievalResult(
            franchise=fa or fb,
            status="disabled",
            receipts=[],
            exhibits=[],
            retrieval_seconds=round(time.monotonic() - t0, 4),
        )
    if not fa and not fb:
        return RetrievalResult(
            franchise=None,
            status="unlisted",
            receipts=[],
            exhibits=[],
            retrieval_seconds=round(time.monotonic() - t0, 4),
        )

    # Same key (or only one listed): one retrieve for both sides.
    if not fa or not fb or fa == fb:
        key = fa or fb
        assert key is not None
        result = retrieve(
            side_a,
            side_b,
            context,
            franchise_hint=key,
            budget_seconds=_left(),
        )
        result.retrieval_seconds = round(time.monotonic() - t0, 4)
        return result

    # Distinct franchises: per-side retrieves sharing the wall clock.
    parts: list[RetrievalResult] = []
    # Run sequentially with remaining budget so the global ceiling is hard.
    # (Each retrieve() already parallelizes its own source fetches.)
    if _left() > 0:
        parts.append(
            retrieve(side_a, "", context, franchise_hint=fa, budget_seconds=_left())
        )
    else:
        parts.append(
            RetrievalResult(
                franchise=fa, status="unavailable", receipts=[], exhibits=[]
            )
        )
    if _left() > 0:
        parts.append(
            retrieve("", side_b, context, franchise_hint=fb, budget_seconds=_left())
        )
    else:
        parts.append(
            RetrievalResult(
                franchise=fb, status="unavailable", receipts=[], exhibits=[]
            )
        )
    merged = _merge_retrieval_results(parts)
    merged.retrieval_seconds = round(time.monotonic() - t0, 4)
    return merged


def clear_retrieval_cache() -> None:
    _CACHE.clear()


def pack_retrieval_for_prompt(result: RetrievalResult) -> str:
    """Format receipts/exhibits for the judge user message."""
    lines: list[str] = [f"RETRIEVAL_STATUS: {result.status}"]
    if result.franchise:
        lines.append(f"FRANCHISE: {result.franchise}")
    if result.retrieval_unavailable:
        lines.append("Retrieval unavailable or franchise unlisted. Put missing canon in unknowns (legal plea). Do NOT invent URLs. Confidence will be capped at 5/10.")
    if result.receipts:
        lines.append("RECEIPTS (autonomous fetch — cite these for load-bearing ruling/concessions):")
        for p in result.receipts:
            lines.append(f"- [{p.retrieval_id}] {p.locator} | {p.source_url}\n  snippet: {p.snippet}")
    if result.exhibits:
        lines.append("EXHIBITS (user-pasted — verify against allowlist; flag unverified):")
        for i, p in enumerate(result.exhibits, start=1):
            flag = "verified" if p.verified else "unverified"
            lines.append(f"- [ex_{i}] ({flag}) {p.claim}\n  snippet: {p.snippet}" + (f" | {p.source_url}" if p.source_url else ""))
    if not result.receipts and not result.exhibits:
        lines.append("No receipts or exhibits packed.")
    return "\n".join(lines)
