"""Franchise allowlist + hard-no checks (config/sources.json)."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "sources.json"


@lru_cache(maxsize=4)
def load_sources_config(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def reload_sources_config(path: str | None = None) -> dict[str, Any]:
    load_sources_config.cache_clear()
    return load_sources_config(path)


def hard_no_patterns(cfg: dict[str, Any] | None = None) -> list[str]:
    cfg = cfg or load_sources_config()
    return [str(x).lower() for x in cfg.get("hard_no", [])]


def is_hard_no_url(url: str, cfg: dict[str, Any] | None = None) -> bool:
    """True if URL host/path matches a hard-no pattern."""
    cfg = cfg or load_sources_config()
    lowered = (url or "").lower()
    host = ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    for pat in hard_no_patterns(cfg):
        if pat in lowered or (host and pat in host):
            return True
    return False


def is_hard_no_text(text: str, cfg: dict[str, Any] | None = None) -> bool:
    """True if free text references a hard-no source class."""
    cfg = cfg or load_sources_config()
    lowered = (text or "").lower()
    for pat in hard_no_patterns(cfg):
        if pat in lowered:
            return True
    return False


def detect_franchise(
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
    franchise_hint: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> str | None:
    """Return franchise key if mapped, else None (unlisted → no retrieval)."""
    cfg = cfg or load_sources_config()
    franchises: dict[str, Any] = cfg.get("franchises") or {}

    if franchise_hint:
        key = franchise_hint.strip().lower().replace(" ", "_").replace("-", "_")
        # direct key
        if key in franchises:
            return key
        # match label / aliases
        for fk, meta in franchises.items():
            label = str(meta.get("label", "")).lower()
            aliases = [a.lower() for a in meta.get("aliases", [])]
            if key == label.replace(" ", "_") or franchise_hint.strip().lower() in aliases:
                return fk
            if franchise_hint.strip().lower() == label:
                return fk

    hay = " ".join(
        x for x in (fighter_a, fighter_b, context or "") if x
    ).lower()

    # Prefer longer alias matches. Short tokens use word boundaries so
    # "hit" does not match "White" and "got" does not match "Goten".
    best: tuple[int, str] | None = None
    for fk, meta in franchises.items():
        aliases = [str(a).lower() for a in meta.get("aliases", [])]
        label = str(meta.get("label", "")).lower()
        candidates = aliases + ([label] if label else [])
        for alias in candidates:
            if alias and _alias_in_text(alias, hay):
                score = len(alias)
                if best is None or score > best[0]:
                    best = (score, fk)
    return best[1] if best else None


def _alias_in_text(alias: str, hay: str) -> bool:
    """Whole-token match; multi-word aliases still match as a phrase."""
    if not alias or not hay:
        return False
    escaped = re.escape(alias)
    return re.search(rf"(?<!\w){escaped}(?!\w)", hay) is not None


def franchise_sources(franchise_key: str, cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_sources_config()
    meta = (cfg.get("franchises") or {}).get(franchise_key) or {}
    sources = list(meta.get("sources") or [])
    # Drop any misconfigured hard-no bases
    out = []
    for s in sources:
        base = str(s.get("base_url", ""))
        if base and not is_hard_no_url(base, cfg):
            out.append(s)
    return out


def snippet_max_words(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or load_sources_config()
    return int(cfg.get("snippet_max_words", 25))


def stale_days(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or load_sources_config()
    return int(cfg.get("stale_days", 90))


def cache_ttl_seconds(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or load_sources_config()
    return int(cfg.get("cache_ttl_seconds", 86400))


def cap_snippet(text: str, max_words: int | None = None) -> str:
    """Cap snippet to N words (default from config, 25). No full quotes."""
    n = max_words if max_words is not None else snippet_max_words()
    words = (text or "").split()
    if len(words) <= n:
        return " ".join(words)
    return " ".join(words[:n])


def url_allowed(url: str, franchise_key: str | None, cfg: dict[str, Any] | None = None) -> bool:
    """URL must not be hard-no, and if franchise set must be under an allowlisted base."""
    cfg = cfg or load_sources_config()
    if not url or is_hard_no_url(url, cfg):
        return False
    if not franchise_key:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for src in franchise_sources(franchise_key, cfg):
        base = str(src.get("base_url", ""))
        try:
            bhost = (urlparse(base).hostname or "").lower()
        except Exception:
            bhost = ""
        if bhost and (host == bhost or host.endswith("." + bhost)):
            return True
    return False


def is_stale(retrieved_at: str | None, cfg: dict[str, Any] | None = None) -> bool:
    """True if retrieved_at is older than stale_days (default 90)."""
    if not retrieved_at:
        return False
    from datetime import datetime, timezone, timedelta

    raw = retrieved_at.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
    return age > timedelta(days=stale_days(cfg))
