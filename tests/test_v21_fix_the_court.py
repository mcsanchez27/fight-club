"""V2.1 — Fix the Court: B7/B11/B12/B8/B1/B2/B2a/B4/T1 (+ shape A)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bot.config import (
    CONFIG_KEYS,
    allow_self_fight_enabled_somewhere,
    get_guild_config,
    set_config,
)
from bot.db import CourtDB
from bot.embeds import challenge_card_embed, ruling_drop_embed, verdict_embed
from bot.fights import (
    MISSING_FIGHT_PROMPT,
    OPEN_ENDED_SIDE_PROMPT,
    accept_fight,
    actor_may_accept,
    create_proposed_fight,
    validate_fight_fields,
)
from bot.judge import (
    UNVERIFIED_CONFIDENCE_CAP,
    apply_retrieval_guardrails,
    validate_verdict,
)
from bot.retrieval import (
    Passage,
    RetrievalResult,
    _first_article_url_from_search_html,
    _html_url_is_verified,
    _is_nav_boilerplate,
    _looks_like_article_snippet,
    _strip_wiki_nav_chrome,
    check_allowlisted_sources,
    log_allowlisted_source_health,
    passage_supports_claim,
)
from bot.sources import load_sources_config, mediawiki_api_url


FROZEN = "2026-09-13T12:00:00Z"


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _verdict(**overrides):
    v = {
        "matchup": "Aragorn vs Goku",
        "steelman_a": "a",
        "steelman_b": "b",
        "concessions": ["speed"],
        "unknowns": [],
        "ruling": "A wins on logistics.",
        "winner": "Aragorn",
        "winner_side": "a",
        "confidence": 9,
        "citations": [],
    }
    v.update(overrides)
    return validate_verdict(v)


# --- B7 LotR api_path ---


def test_b7_lotr_source_uses_w_api_path() -> None:
    cfg = load_sources_config()
    lotr = cfg["franchises"]["lotr"]["sources"][0]
    assert lotr["base_url"] == "https://tolkiengateway.net"
    assert lotr.get("api_path") == "/w/api.php"
    assert mediawiki_api_url(lotr["base_url"], lotr.get("api_path")).endswith(
        "/w/api.php"
    )
    # Default path for Fandom-style wikis
    assert mediawiki_api_url("https://dragonball.fandom.com", None).endswith("/api.php")


def test_b7_check_allowlisted_sources_reports_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):
        calls.append(url)
        if "/w/api.php" in url or url.rstrip("/").endswith("/api.php") or "api.php?" in url:
            return b'{"query":{"general":{"sitename":"TestWiki"}}}'
        return b"<html>ok</html>"

    monkeypatch.setattr("bot.retrieval._http_get", fake_get)
    rows = check_allowlisted_sources(timeout=1.0)
    assert len(rows) >= 4
    assert all("ok" in r and "name" in r for r in rows)
    # Tolkien probe must hit /w/api.php
    assert any("/w/api.php" in u for u in calls)


# --- B12 nav chrome ---


def test_b12_fandom_nav_chrome_stripped_and_rejected() -> None:
    chrome = (
        "Articles on Goku Introduction • Biography • Power and Abilities • "
        "Misc • Gallery"
    )
    assert _is_nav_boilerplate(chrome) is True
    assert _looks_like_article_snippet(chrome, "Goku") is False

    raw = (
        chrome
        + ' This article is about the original character. For other uses, see '
        'Goku (disambiguation). "No, see, I don\'t think of it like I\'m saving '
        "the world. The fact is, it's because I'm usually trying to challenge "
        'the strongest warriors I can find."'
    )
    body = _strip_wiki_nav_chrome(raw)
    assert "Introduction • Biography" not in body
    assert "saving the world" in body
    assert _is_nav_boilerplate(body) is False
    # Body prose after chrome strip is article text (title match supplies the name).
    assert len(body.split()) >= 12


def test_b12_verified_requires_body_support() -> None:
    junk = "Introduction • Biography • Power and Abilities • Gallery"
    assert passage_supports_claim(junk, "Goku (wiki extract)", query="Goku") is False


# --- B11 confidence cap ---


def test_b11_cap_not_set_preserves_low_and_allows_high() -> None:
    # Thin / unavailable: cap high values, leave already-low alone.
    low = apply_retrieval_guardrails(
        _verdict(confidence=3.5),
        RetrievalResult(franchise=None, status="unlisted", receipts=[], exhibits=[]),
    )
    assert low["confidence"] == 3.5

    high = apply_retrieval_guardrails(
        _verdict(confidence=9.0),
        RetrievalResult(
            franchise="dragon_ball", status="unavailable", receipts=[], exhibits=[]
        ),
    )
    assert high["confidence"] == UNVERIFIED_CONFIDENCE_CAP

    # Well-sourced: keep high confidence (no flatten).
    receipt = Passage(
        claim="wiki",
        source_url="https://dragonball.fandom.com/wiki/Goku",
        locator="DB",
        snippet="Goku is a Saiyan raised on Earth who protects his friends",
        verified=True,
        retrieved_at="2026-09-11T12:00:00Z",
        kind="receipt",
    )
    sourced = apply_retrieval_guardrails(
        _verdict(confidence=8.5),
        RetrievalResult(
            franchise="dragon_ball", status="ok", receipts=[receipt], exhibits=[]
        ),
    )
    assert sourced["confidence"] == 8.5
    assert sourced.get("voided") in (None, False)

    # Status ok but no verified receipts → still capped (thin evidence).
    thin = apply_retrieval_guardrails(
        _verdict(confidence=7.0),
        RetrievalResult(
            franchise="dragon_ball",
            status="ok",
            receipts=[
                Passage(
                    claim="nav",
                    source_url="https://dragonball.fandom.com/wiki/Goku",
                    locator="DB",
                    snippet="Introduction Biography Gallery",
                    verified=False,
                    retrieved_at="2026-09-11T12:00:00Z",
                    kind="receipt",
                )
            ],
            exhibits=[],
        ),
    )
    assert thin["confidence"] == UNVERIFIED_CONFIDENCE_CAP


# --- B8 footer ---


def test_b8_footer_omits_voided_on_valid_instant() -> None:
    v = _verdict(confidence=7.0, retrieval_status="ok", voided=False)
    embed = verdict_embed(v)
    assert "voided" not in (embed.footer.text or "").lower()

    v2 = _verdict(confidence=4.5, retrieval_status="unavailable")
    # Guardrails no longer stamp voided; simulate enqueue path without voided on card.
    embed2 = verdict_embed(v2)
    footer = (embed2.footer.text or "").lower()
    assert "voided" not in footer
    assert "re-judge" in footer or "receipts: unavailable" in footer


# --- B1 / B2 / B2a embeds ---


def test_b2_title_names_fight_and_winner() -> None:
    v = _verdict(matchup="", winner="B", winner_side="b", confidence=6.0)
    fight = {"side_a": "Aragorn", "side_b": "Geralt"}
    embed = verdict_embed(v, fight=fight)
    assert "Aragorn vs Geralt" in (embed.title or "")
    assert "—" not in (embed.title or "").replace("⚔", "")
    assert "Geralt" in embed.fields[0].value
    assert "6.0/10" in embed.fields[0].value or "6/10" in embed.fields[0].value


def test_b1_ruling_drop_is_compact() -> None:
    v = _verdict(
        steelman_a="A" * 800,
        steelman_b="B" * 800,
        concessions=["one", "two", "three", "four"],
        unknowns=["u1", "u2", "u3", "u4"],
        ruling="Sentence one. Sentence two. Sentence three.",
    )
    fight = {"side_a": "Goku", "side_b": "Vegeta"}
    embed = ruling_drop_embed(v, fight=fight)
    assert "Goku" in (embed.title or "") or "⚖" in (embed.title or "")
    names = [f.name for f in embed.fields]
    assert "Verdict" in names
    # Steelmans clipped well under Discord field limit so the card doesn't end in …
    sa = embed.fields[names.index("Steelman A")].value
    assert len(sa) <= 321


def test_challenge_card_echoes_matchup() -> None:
    fight = {
        "id": 7,
        "side_a": "Aragorn",
        "side_b": "Geralt",
        "challenger_id": 1,
        "challengee_id": 2,
        "open_ended": False,
    }
    embed = challenge_card_embed(fight)
    assert "Aragorn vs Geralt" in (embed.title or "")
    assert "Matchup:" in (embed.description or "")
    assert "Aragorn vs Geralt" in (embed.description or "")


# --- B4 error copy ---


def test_b4_open_ended_error_copy_readable() -> None:
    assert "champion_a" in OPEN_ENDED_SIDE_PROMPT
    assert "side:" not in OPEN_ENDED_SIDE_PROMPT
    assert "champion_a" in MISSING_FIGHT_PROMPT
    assert "champion_b" in MISSING_FIGHT_PROMPT
    plan = validate_fight_fields(opponent_id=1)
    assert plan.kind == "prompt"
    assert "champion_a" in (plan.prompt or "")


# --- B3/B9/B10 shape A ---


def test_shape_a_champions_no_silent_override() -> None:
    plan = validate_fight_fields(
        opponent_id=2, champion_a="Geralt", champion_b="Aragorn"
    )
    assert plan.side_a == "Geralt" and plan.side_b == "Aragorn"


# --- T1 allow_self_fight ---


def test_t1_allow_self_fight_default_off(tmp_path: Path) -> None:
    assert "allow_self_fight" in CONFIG_KEYS
    db = _db(tmp_path)
    assert get_guild_config(db, 1, "allow_self_fight") is False
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN,
    )
    assert actor_may_accept(fight, 10, db) is False
    assert actor_may_accept(fight, 20, db) is True
    with pytest.raises(ValueError, match="challenged"):
        accept_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    db.close()


def test_t1_allow_self_fight_enables_challenger_accept(tmp_path: Path) -> None:
    db = _db(tmp_path)
    set_config(db, 1, "allow_self_fight", "true")
    assert get_guild_config(db, 1, "allow_self_fight") is True
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="A",
        side_b="B",
        context=None,
        now=FROZEN,
    )
    assert actor_may_accept(fight, 10, db) is True
    out = accept_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    assert out["status"] == "arguing"
    db.close()


# --- Prime follow-up nits ---


def test_verdict_embed_passes_thin_record_bit() -> None:
    """verdict_embed must not hardcode thin=False; banner survives via verdict bit."""
    from bot.embeds import THIN_RECORD_BANNER

    v = _verdict(
        thin_record=True,
        thin_record_banner=THIN_RECORD_BANNER,
        ruling="A wins on the available record.",
    )
    embed = verdict_embed(v)
    assert THIN_RECORD_BANNER in (embed.description or "")
    assert "thin record" in (embed.footer.text or "").lower()


def test_html_search_resolves_first_article_url() -> None:
    base = "https://www.kanzenshuu.com"
    html = """
    <html><body>
      <a href="/?s=Goku">Search again</a>
      <a href="/tag/saiyan">Tag</a>
      <a href="https://evil.example/wiki/Goku">Offsite</a>
      <a href="/guides/goku-power-levels">Goku power levels</a>
      <a href="/guides/other">Other</a>
    </body></html>
    """
    got = _first_article_url_from_search_html(html, base)
    assert got == f"{base}/guides/goku-power-levels"
    assert _html_url_is_verified(got, base) is True
    assert _first_article_url_from_search_html("<a href='/?s=x'>x</a>", base) is None


def test_source_health_debounced(monkeypatch) -> None:
    import bot.retrieval as retrieval

    monkeypatch.setattr(retrieval, "_SOURCE_HEALTH_LAST_MONO", None)
    monkeypatch.setattr(retrieval, "_SOURCE_HEALTH_TTL_SECONDS", 3600.0)
    calls = {"n": 0}

    def fake_check(*, timeout: float = 5.0):
        calls["n"] += 1
        return [
            {
                "franchise": "x",
                "name": "y",
                "base_url": "https://example.com",
                "ok": True,
                "detail": "ok",
            }
        ]

    monkeypatch.setattr(retrieval, "check_allowlisted_sources", fake_check)
    first = log_allowlisted_source_health(timeout=1.0)
    second = log_allowlisted_source_health(timeout=1.0)
    assert calls["n"] == 1
    assert len(first) == 1
    assert second == []
    third = log_allowlisted_source_health(timeout=1.0, force=True)
    assert calls["n"] == 2
    assert len(third) == 1


def test_t1_allow_self_fight_opponent_is_me_both_champions(tmp_path: Path) -> None:
    """Self-fight (challenger==challengee) with both champions named may Accept."""
    db = _db(tmp_path)
    set_config(db, 1, "allow_self_fight", "true")
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=10,  # opponent == me
        side_a="Goku",
        side_b="Vegeta",
        context=None,
        now=FROZEN,
    )
    assert actor_may_accept(fight, 10, db) is True
    out = accept_fight(db, int(fight["id"]), now=FROZEN, actor_id=10)
    assert out["status"] == "arguing"
    assert allow_self_fight_enabled_somewhere(db) is True
    db.close()
