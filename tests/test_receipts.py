"""Phase 3 receipts: allowlist, snippet cap, confidence cap, citations, export."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bot.db import CourtDB
from bot.export import export_markdown
from bot.judge import (
    UNVERIFIED_CONFIDENCE_CAP,
    apply_retrieval_guardrails,
    normalize_citation,
    validate_verdict,
)
from bot.retrieval import (
    Passage,
    RetrievalResult,
    _looks_like_article_snippet,
    exhibit_from_text,
)
from bot.sources import (
    cap_snippet,
    detect_franchise,
    is_hard_no_text,
    is_hard_no_url,
    load_sources_config,
    url_allowed,
)


def _verdict(**overrides):
    v = {
        "matchup": "A vs B",
        "steelman_a": "a",
        "steelman_b": "b",
        "concessions": ["speed"],
        "unknowns": [],
        "ruling": "A wins",
        "winner": "A",
        "confidence": 9,
        "citations": [],
    }
    v.update(overrides)
    return validate_verdict(v)


def test_hard_no_urls_and_text() -> None:
    cfg = load_sources_config()
    assert is_hard_no_url("https://vsbattles.fandom.com/wiki/Goku", cfg)
    assert is_hard_no_url("https://www.reddit.com/r/whowouldwin", cfg)
    assert is_hard_no_url("https://www.youtube.com/watch?v=abc", cfg)
    assert is_hard_no_text("see the vs battles tier list", cfg)
    assert not is_hard_no_url("https://dragonball.fandom.com/wiki/Goku", cfg)
    assert not url_allowed("https://vsbattles.fandom.com/wiki/Goku", "dragon_ball", cfg)
    assert url_allowed("https://dragonball.fandom.com/wiki/Goku", "dragon_ball", cfg)


def test_snippet_25_word_cap() -> None:
    words = " ".join(f"w{i}" for i in range(40))
    capped = cap_snippet(words, 25)
    assert len(capped.split()) == 25
    assert cap_snippet("short one", 25) == "short one"
    c = normalize_citation({"claim": "x", "snippet": words})
    assert len(c["snippet"].split()) == 25


def test_confidence_capped_when_retrieval_unavailable() -> None:
    v = _verdict(confidence=9)
    result = RetrievalResult(franchise=None, status="unlisted", receipts=[], exhibits=[])
    out = apply_retrieval_guardrails(v, result)
    assert out["confidence"] <= UNVERIFIED_CONFIDENCE_CAP
    assert out["confidence"] == 5.0
    assert out["voided"] is True
    assert any("retrieval unavailable" in u for u in out["unknowns"])


def test_confidence_capped_when_fetch_failed() -> None:
    v = _verdict(confidence=8.5)
    result = RetrievalResult(franchise="dragon_ball", status="unavailable", receipts=[], exhibits=[])
    out = apply_retrieval_guardrails(v, result)
    assert out["confidence"] == 5.0
    assert out["retrieval_status"] == "unavailable"


def test_citations_table_round_trip(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    rid = db.insert_ruling(message_id=1, channel_id=2, guild_id=3, fighter_a="Goku", fighter_b="Vegeta", context=None, verdict=_verdict(), franchise="dragon_ball", retrieval_status="ok", voided=False)
    cid = db.insert_citation(ruling_id=rid, claim="Goku can go Super Saiyan", source_url="https://dragonball.fandom.com/wiki/Goku", locator="Dragon Ball Wiki · Goku", snippet="Goku is a Saiyan raised on Earth", verified=True, retrieved_at="2026-09-11T12:00:00Z", kind="receipt")
    assert cid >= 1
    db.insert_citation(ruling_id=rid, claim="User says Vegeta wins in the Oozaru form", source_url=None, locator="user exhibit", snippet="Vegeta as Great Ape stomps", verified=False, retrieved_at="2026-09-11T12:00:01Z", kind="exhibit")
    rows = db.list_citations(rid)
    assert len(rows) == 2
    assert rows[0]["verified"] is True
    assert rows[0]["kind"] == "receipt"
    assert rows[1]["kind"] == "exhibit"
    db.close()


def test_export_markdown_shape() -> None:
    v = _verdict(citations=[{"claim": "Broly ramps", "source_url": "https://dragonball.fandom.com/wiki/Broly", "locator": "Power", "snippet": "His power increases the longer he fights", "verified": True, "kind": "receipt"}])
    md = export_markdown(v, fighter_a="Broly", fighter_b="Goku", context="open field", franchise="dragon_ball", retrieval_status="ok")
    assert md.startswith("```markdown\n")
    assert md.rstrip().endswith("```")
    assert "## Ruling" in md and "receipt/verified" in md


def test_export_shows_retrieval_unavailable_banner() -> None:
    md = export_markdown(_verdict(confidence=5), retrieval_status="unlisted", voided=True)
    assert "unverified: retrieval unavailable" in md
    assert "voided" in md.lower()


def test_detect_franchise_aliases() -> None:
    assert detect_franchise("Goku", "Vegeta") == "dragon_ball"
    assert detect_franchise("Jon Snow", "White Walker") == "asoiaf"
    assert detect_franchise("Gandalf", "Sauron") == "lotr"
    assert detect_franchise("Ragnar", "Lagertha") == "vikings"
    assert detect_franchise("Batman", "Superman") is None


def test_detect_franchise_ignores_substring_aliases() -> None:
    assert detect_franchise("Superman", "White Lantern") is None
    assert detect_franchise("Goten", "Batman") is None
    assert detect_franchise("Cancelled plans", "Joker") is None
    assert detect_franchise("I forgot", "Batman") is None
    assert detect_franchise("Restart protocol", "Batman") is None
    assert detect_franchise("Cell", "Batman") == "dragon_ball"
    assert detect_franchise("Goku", "Superman") == "dragon_ball"


def test_exhibit_unverified_without_allowlisted_url() -> None:
    p = exhibit_from_text("Goku blinked and won", franchise_key="dragon_ball")
    assert p.kind == "exhibit"
    assert p.verified is False


def test_hard_no_url_stripped_from_exhibit() -> None:
    p = exhibit_from_text("see this", franchise_key="dragon_ball", source_url="https://vsbattles.fandom.com/wiki/Goku")
    assert p.verified is False
    assert p.source_url == ""


def test_rejudge_queue_round_trip(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    rid = db.insert_ruling(message_id=9, channel_id=1, guild_id=1, fighter_a="A", fighter_b="B", context=None, verdict=_verdict(confidence=5), retrieval_status="unavailable", voided=True)
    db.enqueue_rejudge(rid, reason="retrieval_status=unavailable")
    assert db.list_pending_rejudges()[0]["ruling_id"] == rid
    db.mark_rejudge_done(rid, status="done")
    assert db.list_pending_rejudges() == []
    db.close()


def test_apply_merges_packed_receipts() -> None:
    receipt = Passage(claim="wiki claim", source_url="https://dragonball.fandom.com/wiki/Goku", locator="DB Wiki · Goku", snippet="Goku is a Saiyan", verified=True, retrieved_at="2026-09-11T12:00:00Z", kind="receipt", retrieval_id="ret_1")
    out = apply_retrieval_guardrails(_verdict(citations=["model string cite"]), RetrievalResult(franchise="dragon_ball", status="ok", receipts=[receipt], exhibits=[]))
    assert out["confidence"] == 9.0
    assert any(c.get("claim") == "wiki claim" for c in out["citations"])


def test_html_search_chrome_is_rejected() -> None:
    chrome = "You searched for Goku – Kanzenshuu Forum Wiki News General Info FAQs Features From the Past Press Archive Reviews"
    assert _looks_like_article_snippet(chrome, "Goku") is False
    article = "Goku is a Saiyan raised on Earth who trains under Master Roshi and later fights Frieza to defend Namek."
    assert _looks_like_article_snippet(article, "Goku") is True


def test_stale_receipt_unverified() -> None:
    c = normalize_citation({"claim": "old claim", "snippet": "old snippet words here", "verified": True, "retrieved_at": "2020-01-01T00:00:00Z", "kind": "receipt"})
    assert c["verified"] is False
    assert c.get("stale") is True
