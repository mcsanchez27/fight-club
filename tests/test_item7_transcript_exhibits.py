"""V2 item 7 — transcript assembly, exhibit extraction/verification, ❌ contest."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.db import CourtDB
from bot.exhibits import (
    contest_exhibits_on_message,
    extract_exhibits_for_fight,
    extract_quotes,
    extract_urls,
    format_exhibit_ledger,
    prepare_judge_materials,
    quote_verified_against_receipts,
)
from bot.fights import (
    JUDGE_READY_STATUS,
    accept_fight,
    create_proposed_fight,
    mark_judge_ready,
    rest_fight,
)
from bot.transcript import (
    assemble_transcript,
    build_and_store_transcript_snapshot,
    filter_advocate_messages,
    snapshot_text,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _arguing(db: CourtDB, *, thread_id: int = 9001) -> dict:
    fight = create_proposed_fight(
        db,
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Goku",
        side_b="Vegeta",
        context="open field",
        now=FROZEN,
    )
    fid = int(fight["id"])
    db.update_fight(fid, franchise_a="dragon_ball", franchise_b="dragon_ball")
    out = accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=thread_id)
    assert out["status"] == "arguing"
    return db.get_fight(fid)  # type: ignore[return-value]


def _msg(
    mid: int,
    author_id: int,
    content: str,
    *,
    attachments: list | None = None,
) -> dict:
    return {
        "id": mid,
        "author_id": author_id,
        "content": content,
        "attachments": attachments or [],
    }


# --- Transcript assembly -----------------------------------------------------


def test_advocate_filter_and_labeling(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    messages = [
        _msg(1, 10, "Opening for Goku."),
        _msg(2, 99, "gallery noise — ignore"),
        _msg(3, 20, "Vegeta answers."),
        _msg(4, 10, "Closing A."),
    ]
    filtered = filter_advocate_messages(fight, messages)
    assert [t["side"] for t in filtered] == ["a", "b", "a"]
    assert filtered[0]["label"] == "[A:Goku]"
    assert filtered[1]["label"] == "[B:Vegeta]"
    snap = assemble_transcript(fight, messages, max_tokens=20_000)
    assert snap["lines"][0].startswith("[A:Goku] Opening")
    assert snap["lines"][1].startswith("[B:Vegeta] Vegeta")
    assert "gallery" not in snap["text"]
    db.close()


def test_middle_truncation_keeps_openings_and_closings(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    # Many long messages per side so token cap bites.
    messages: list[dict] = []
    mid = 1
    filler = "word " * 200  # ~200 words → ~250+ tokens per line with label
    for i in range(6):
        messages.append(_msg(mid, 10, f"A-open-{i} {filler}"))
        mid += 1
        messages.append(_msg(mid, 20, f"B-open-{i} {filler}"))
        mid += 1
    snap = assemble_transcript(fight, messages, max_tokens=800)
    assert snap["truncated"] is True
    text = snap["text"]
    # First and last of each side survive.
    assert "A-open-0" in text
    assert "B-open-0" in text
    assert "A-open-5" in text
    assert "B-open-5" in text
    # Some middle dropped.
    middles_a = [f"A-open-{i}" for i in range(1, 5)]
    assert any(m not in text for m in middles_a)
    db.close()


def test_snapshot_frozen_after_store(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    messages = [
        _msg(1, 10, "Alpha opening argument here."),
        _msg(2, 20, "Bravo replies with strength."),
    ]
    snap = build_and_store_transcript_snapshot(db, fight, messages)
    # Mutate input list after store.
    messages[0]["content"] = "MUTATED SHOULD NOT APPEAR"
    messages.append(_msg(3, 10, "late add"))
    stored = db.get_fight(int(fight["id"]))
    assert stored is not None
    assert stored["transcript_snapshot"]["text"] == snap["text"]
    assert "MUTATED" not in stored["transcript_snapshot"]["text"]
    assert "late add" not in stored["transcript_snapshot"]["text"]
    # Returned snap is also independent.
    snap["text"] = "poison"
    stored2 = db.get_fight(int(fight["id"]))
    assert stored2 is not None
    assert "poison" not in snapshot_text(stored2["transcript_snapshot"])
    assert "Alpha opening" in snapshot_text(stored2["transcript_snapshot"])
    db.close()


def test_mark_judge_ready_stages_snapshot_and_exhibits(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    rest_fight(db, fid, now=FROZEN, actor_id=10)
    messages = [
        _msg(1, 10, 'See https://dragonball.fandom.com/wiki/Goku and "Goku can turn Super Saiyan in moments under pressure tonight."'),
        _msg(2, 20, "Vegeta disagrees."),
    ]
    ok = mark_judge_ready(
        db,
        fid,
        messages,
        fetch_url=lambda url: url,  # mock allowlist fetch success
    )
    assert ok is True
    row = db.get_fight(fid)
    assert row is not None
    assert row["status"] == JUDGE_READY_STATUS
    assert row["transcript_snapshot"] is not None
    assert "[A:Goku]" in row["transcript_snapshot"]["text"]
    exhibits = db.list_exhibits(fid)
    assert exhibits
    assert all(e["source_role"] == "advocate" for e in exhibits)
    assert not any(e["source_role"] == "gallery" for e in exhibits)
    # Second call does not re-stage / double-transition
    assert mark_judge_ready(db, fid, messages) is False
    db.close()


# --- Exhibit extraction / verification ---------------------------------------


def test_extract_urls_and_quotes() -> None:
    text = (
        'Look at https://example.com/x and “This quote has eight whole words in it now yes.” '
        'Also "short".'
    )
    assert extract_urls(text) == ["https://example.com/x"]
    quotes = extract_quotes(text)
    assert len(quotes) == 1
    assert "eight whole words" in quotes[0]


def test_exhibit_kinds_quote_link_image(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    eight = "one two three four five six seven eight"
    messages = [
        _msg(
            1,
            10,
            f'Link https://dragonball.fandom.com/wiki/Goku and "{eight} extras here."',
            attachments=[
                {
                    "url": "https://cdn.discordapp.com/a.png",
                    "filename": "a.png",
                    "content_type": "image/png",
                }
            ],
        ),
        _msg(2, 99, "gallery should never become a row"),
    ]
    rows = extract_exhibits_for_fight(
        db, fight, messages, fetch_url=lambda u: u, replace=True
    )
    kinds = sorted(r["kind"] for r in rows)
    assert kinds == ["image", "link", "quote"]
    assert all(r["source_role"] == "advocate" for r in rows)
    img = next(r for r in rows if r["kind"] == "image")
    assert img["status"] == "unverified"
    link = next(r for r in rows if r["kind"] == "link")
    assert link["status"] == "verified"
    assert link["verified_url"]
    # No gallery rows even if non-advocate posted.
    assert len(rows) == 3
    db.close()


def test_quote_verify_hit_and_miss(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    receipt_quote = (
        "Goku can turn Super Saiyan in moments under extreme pressure today"
    )
    db.insert_receipt(
        fight_id=fid,
        claim="Goku SSJ",
        snippet=receipt_quote,
        verified=True,
        source_url="https://dragonball.fandom.com/wiki/Goku",
    )
    hit = (
        "Goku can turn Super Saiyan in moments under extreme pressure today"
    )
    miss = "This quote has eight whole words but matches nothing stored"
    assert quote_verified_against_receipts(hit, db.list_receipts(fid)) is True
    assert quote_verified_against_receipts(miss, db.list_receipts(fid)) is False

    messages = [
        _msg(1, 10, f'Hit: "{hit}" Miss: "{miss}"'),
    ]
    rows = extract_exhibits_for_fight(db, fight, messages, fetch_url=lambda u: None)
    quotes = [r for r in rows if r["kind"] == "quote"]
    assert len(quotes) == 2
    statuses = {r["text"]: r["status"] for r in quotes}
    assert statuses[hit] == "verified"
    assert statuses[miss] == "unverified"
    db.close()


def test_link_allowlist_and_offlist(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    messages = [
        _msg(
            1,
            10,
            "Good https://dragonball.fandom.com/wiki/Vegeta bad https://evil.example/nope",
        ),
    ]
    rows = extract_exhibits_for_fight(
        db, fight, messages, fetch_url=lambda u: u if "fandom" in u else None
    )
    by_url = {r["url"]: r for r in rows if r["kind"] == "link"}
    assert by_url["https://dragonball.fandom.com/wiki/Vegeta"]["status"] == "verified"
    assert by_url["https://evil.example/nope"]["status"] == "unverified"
    assert by_url["https://evil.example/nope"]["verified_url"] is None
    db.close()


def test_image_always_unverified_no_gallery(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    messages = [
        _msg(
            1,
            10,
            "pic",
            attachments=[
                {"url": "https://cdn/x.jpg", "filename": "x.jpg", "content_type": "image/jpeg"}
            ],
        ),
        _msg(
            2,
            20,
            "also",
            attachments=[
                {"url": "https://cdn/y.webp", "filename": "y.webp", "content_type": "image/webp"}
            ],
        ),
    ]
    rows = extract_exhibits_for_fight(db, fight, messages)
    assert len(rows) == 2
    assert all(r["kind"] == "image" and r["status"] == "unverified" for r in rows)
    assert all(r["source_role"] == "advocate" for r in rows)
    ledger = format_exhibit_ledger(rows)
    assert "advocate posted an image" in ledger
    db.close()


# --- Contest -----------------------------------------------------------------


def test_contest_overrides_verified(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    messages = [
        _msg(42, 10, "https://dragonball.fandom.com/wiki/Goku"),
    ]
    rows = extract_exhibits_for_fight(
        db, fight, messages, fetch_url=lambda u: u
    )
    assert rows[0]["status"] == "verified"
    updated = contest_exhibits_on_message(
        db, fight, message_id=42, reactor_id=20  # opposing advocate B
    )
    assert updated
    assert all(e["status"] == "contested" for e in updated)
    # Own reaction does nothing
    again = contest_exhibits_on_message(
        db, fight, message_id=42, reactor_id=10
    )
    assert again == []
    # Still contested
    assert db.list_exhibits(int(fight["id"]))[0]["status"] == "contested"
    # Spectator ignored
    assert (
        contest_exhibits_on_message(db, fight, message_id=42, reactor_id=99)
        == []
    )
    db.close()


def test_prepare_judge_materials_bundle(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    messages = [
        _msg(1, 10, "A opens the case carefully."),
        _msg(2, 20, "B answers with equal care."),
    ]
    out = prepare_judge_materials(db, int(fight["id"]), messages)
    assert "Goku" in out["snapshot"]["text"]
    assert out["exhibit_ledger"] == "(none)" or isinstance(out["exhibit_ledger"], str)
    row = db.get_fight(int(fight["id"]))
    assert row is not None
    assert row["transcript_snapshot"]["turns"]
    db.close()
