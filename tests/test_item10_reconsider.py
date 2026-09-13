"""V2 item 10 — /reconsider once-only, snapshot reuse, parent_ruling_id, diff."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from bot.db import CourtDB
from bot.embeds import ruling_drop_embed
from bot.fights import (
    RULED_STATUS,
    accept_fight,
    create_proposed_fight,
    rest_fight,
    to_iso,
)
from bot.judge import validate_verdict
from bot.retrieval import Passage, RetrievalResult
from bot.ruling import (
    fight_has_reconsideration,
    format_prior_ruling_block,
    reconsider_fight,
    reconsideration_diff,
    resolve_reconsider_snapshot,
    rule_fight,
)
from bot.transcript import build_and_store_transcript_snapshot


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _msg(mid: int, author_id: int, content: str) -> dict:
    return {"id": mid, "author_id": author_id, "content": content, "attachments": []}


def _mock_verdict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "steelman_a": "Goku scales past.",
        "steelman_b": "Vegeta pride wins.",
        "exhibit_ledger": [],
        "concessions": ["Speed edge to A"],
        "unknowns": [],
        "opening_score_a": None,
        "opening_score_b": None,
        "close_score_a": None,
        "close_score_b": None,
        "ruling": "Goku takes it on logistics.",
        "winner_side": "a",
        "confidence": 7.0,
        "argument_quality": None,
        "citations": [{"claim": "UI dodge", "verified": False}],
        "matchup": "Goku vs Vegeta",
    }
    base.update(overrides)
    return validate_verdict(base)


def _ruled_fight(db: CourtDB, *, thread_id: int = 9001) -> dict:
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
    accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=thread_id)
    rest_fight(db, fid, now=FROZEN, actor_id=10)
    rest_fight(db, fid, now=FROZEN + timedelta(minutes=1), actor_id=20)
    fight = db.get_fight(fid)
    assert fight is not None
    msgs = [
        _msg(1, 10, "Goku opens with UI and stamina."),
        _msg(2, 20, "Vegeta answers with pride and galick."),
    ]
    build_and_store_transcript_snapshot(db, fight, msgs)
    db.insert_receipt(
        fight_id=fid,
        claim="UI",
        source_url="https://dragonball.fandom.com/wiki/Ultra_Instinct",
        locator="Ultra Instinct",
        snippet="Autonomous movement",
        verified=True,
        retrieved_at="2026-09-13T00:00:00Z",
        kind="receipt",
        retrieval_id="r1",
        franchise="dragon_ball",
    )
    db.update_fight(fid, retrieval_status="ok")
    result = rule_fight(
        db,
        fid,
        now=FROZEN + timedelta(minutes=2),
        judge_fn=lambda **kw: _mock_verdict(),
        allow_receipt_refresh=False,
    )
    assert result["already_ruled"] is False
    fight = db.get_fight(fid)
    assert fight is not None
    assert fight["status"] == RULED_STATUS
    return fight


# --- Diff helper -------------------------------------------------------------


def test_reconsideration_diff_unchanged_and_reversed() -> None:
    assert (
        reconsideration_diff("a", 7, "a", 6)
        == "Winner unchanged, confidence 7.0 → 6.0"
    )
    assert (
        reconsideration_diff("a", 7.0, "b", 8.5) == "Winner reversed"
    )
    assert (
        reconsideration_diff("b", "6", "b", "6.0")
        == "Winner unchanged, confidence 6.0 → 6.0"
    )


def test_reconsideration_embed_title_and_diff() -> None:
    v = _mock_verdict(confidence=6.0, winner_side="b", winner="Vegeta")
    embed = ruling_drop_embed(
        v,
        fight={"side_a": "Goku", "side_b": "Vegeta"},
        title="Ruling on reconsideration",
        diff_line="Winner reversed",
    )
    assert embed.title == "Ruling on reconsideration"
    names = [f.name for f in embed.fields]
    assert "Diff" in names
    diff_field = next(f for f in embed.fields if f.name == "Diff")
    assert diff_field.value == "Winner reversed"
    assert "reconsideration" in (embed.footer.text or "").lower()


# --- Core reconsider path ----------------------------------------------------


def test_once_only_enforcement(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    assert fight_has_reconsideration(db, fid) is False

    first = reconsider_fight(
        db,
        fid,
        evidence="Vegeta's pride feat scales past UI.",
        now=FROZEN + timedelta(hours=1),
        actor_id=10,
        judge_fn=lambda **kw: _mock_verdict(winner_side="b", confidence=6.5, winner="Vegeta"),
        allow_receipt_refresh=False,
    )
    assert first["kind"] == "reconsideration"
    assert fight_has_reconsideration(db, fid) is True
    assert db.get_fight(fid)["status"] == RULED_STATUS

    with pytest.raises(ValueError, match="already has a reconsideration"):
        reconsider_fight(
            db,
            fid,
            evidence="Second try should fail.",
            now=FROZEN + timedelta(hours=2),
            actor_id=20,
            judge_fn=lambda **kw: _mock_verdict(),
            allow_receipt_refresh=False,
        )
    # Still exactly one reconsideration row.
    rows = db._conn.execute(
        "SELECT kind FROM rulings WHERE fight_id = ? ORDER BY id",
        (fid,),
    ).fetchall()
    kinds = [r["kind"] for r in rows]
    assert kinds == ["initial", "reconsideration"]
    db.close()


def test_requires_nonempty_evidence_and_ruled_status(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])

    with pytest.raises(ValueError, match="Evidence is required"):
        reconsider_fight(
            db, fid, evidence="   ", now=FROZEN, actor_id=10,
            judge_fn=lambda **kw: _mock_verdict(),
        )
    with pytest.raises(ValueError, match="Evidence is required"):
        reconsider_fight(
            db, fid, evidence="", now=FROZEN, actor_id=10,
            judge_fn=lambda **kw: _mock_verdict(),
        )

    # Non-advocate rejected when actor_id provided.
    with pytest.raises(ValueError, match="Only advocates"):
        reconsider_fight(
            db, fid, evidence="canon cite", now=FROZEN, actor_id=999,
            judge_fn=lambda **kw: _mock_verdict(),
        )

    # Not ruled yet.
    fight2 = create_proposed_fight(
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
    with pytest.raises(ValueError, match="Cannot reconsider"):
        reconsider_fight(
            db,
            int(fight2["id"]),
            evidence="too early",
            now=FROZEN,
            actor_id=10,
            judge_fn=lambda **kw: _mock_verdict(),
        )
    db.close()


def test_snapshot_reuse_no_live_messages(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    prior = db.get_initial_ruling_for_fight(fid)
    assert prior is not None
    snap_before = resolve_reconsider_snapshot(fight, prior)
    assert snap_before is not None
    text_before = snap_before["text"] if isinstance(snap_before, dict) else str(snap_before)
    assert "Goku opens" in text_before

    # Poison the live message list that a naive re-fetch would see — reconsider
    # has no messages parameter and must not depend on Discord history.
    captured: dict[str, Any] = {}

    def capture_judge(**kw: Any) -> dict[str, Any]:
        captured["transcript"] = kw.get("transcript")
        captured["prior_ruling"] = kw.get("prior_ruling")
        return _mock_verdict(confidence=6.0)

    out = reconsider_fight(
        db,
        fid,
        evidence="UI stamina drain was underweighted.",
        now=FROZEN + timedelta(hours=1),
        actor_id=20,
        judge_fn=capture_judge,
        allow_receipt_refresh=False,
    )
    assert "Goku opens" in (captured.get("transcript") or "")
    assert "SHOULD NOT APPEAR" not in (captured.get("transcript") or "")
    assert "PRIOR RULING" in (captured.get("prior_ruling") or "")
    assert "UI stamina drain" in (captured.get("prior_ruling") or "")
    # Stored snapshot unchanged.
    fight2 = db.get_fight(fid)
    assert "Goku opens" in fight2["transcript_snapshot"]["text"]
    assert out["snapshot"]["text"] == text_before
    db.close()


def test_parent_ruling_id_and_kind(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    prior = db.get_initial_ruling_for_fight(fid)
    assert prior is not None
    assert prior["kind"] == "initial"

    out = reconsider_fight(
        db,
        fid,
        evidence="New logistics cite from the manga.",
        now=FROZEN + timedelta(hours=1),
        actor_id=10,
        judge_fn=lambda **kw: _mock_verdict(winner_side="b", confidence=5.5),
        allow_receipt_refresh=False,
    )
    assert out["parent_ruling_id"] == prior["id"]
    row = db.get_ruling_by_id(int(out["ruling_id"]))
    assert row is not None
    assert row["kind"] == "reconsideration"
    assert row["parent_ruling_id"] == prior["id"]
    assert row["fight_id"] == fid
    # Both stay on record; latest is reconsideration.
    latest = db.get_latest_ruling_for_fight(fid)
    assert latest is not None
    assert latest["id"] == row["id"]
    assert latest["kind"] == "reconsideration"
    # Status stays ruled.
    assert db.get_fight(fid)["status"] == RULED_STATUS
    db.close()


def test_diff_line_on_result(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    # Initial was winner_side=a confidence 7.0
    out = reconsider_fight(
        db,
        fid,
        evidence="Pride feat.",
        now=FROZEN + timedelta(hours=1),
        actor_id=10,
        judge_fn=lambda **kw: _mock_verdict(winner_side="a", confidence=6.0),
        allow_receipt_refresh=False,
    )
    assert out["diff_line"] == "Winner unchanged, confidence 7.0 → 6.0"
    assert out["verdict"]["reconsideration_diff"] == out["diff_line"]
    db.close()


def test_receipts_reuse_one_shot_refresh_when_empty(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    db.clear_fight_receipts(fid)
    db.update_fight(fid, retrieval_status="unavailable")

    refreshes = {"n": 0}

    def fake_retrieve(*args, **kwargs):
        refreshes["n"] += 1
        return RetrievalResult(
            franchise="dragon_ball",
            status="ok",
            receipts=[
                Passage(
                    claim="refreshed",
                    source_url="https://dragonball.fandom.com/wiki/Goku",
                    locator="Goku",
                    snippet="The hero",
                    verified=True,
                    retrieved_at="2026-09-13T00:00:00Z",
                    retrieval_id="rr1",
                )
            ],
            exhibits=[],
        )

    out = reconsider_fight(
        db,
        fid,
        evidence="Need receipts for the motion.",
        now=FROZEN + timedelta(hours=1),
        actor_id=10,
        judge_fn=lambda **kw: _mock_verdict(),
        retrieve_fn=fake_retrieve,
        allow_receipt_refresh=True,
    )
    assert refreshes["n"] == 1
    assert out["receipts_refreshed"] is True
    # Second reconsider blocked before any retrieve.
    with pytest.raises(ValueError, match="already has a reconsideration"):
        reconsider_fight(
            db,
            fid,
            evidence="again",
            now=FROZEN + timedelta(hours=2),
            actor_id=10,
            judge_fn=lambda **kw: _mock_verdict(),
            retrieve_fn=fake_retrieve,
        )
    assert refreshes["n"] == 1
    db.close()


def test_format_prior_ruling_block_includes_evidence() -> None:
    prior = {
        "id": 3,
        "kind": "initial",
        "winner_side": "a",
        "confidence": 7.0,
        "verdict": {
            "ruling": "A wins on logistics.",
            "winner": "Goku",
            "winner_side": "a",
            "confidence": 7.0,
            "steelman_a": "fast",
            "steelman_b": "proud",
            "concessions": ["B has pride"],
        },
    }
    block = format_prior_ruling_block(prior, "New manga panel.")
    assert "PRIOR RULING" in block
    assert "ruling_id: 3" in block
    assert "New manga panel." in block
    assert "A wins on logistics." in block


def test_reconsideration_counts_as_latest_for_records(tmp_path: Path) -> None:
    from bot.records import iter_guild_outcomes

    db = _db(tmp_path)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    reconsider_fight(
        db,
        fid,
        evidence="Reverse it.",
        now=FROZEN + timedelta(hours=1),
        actor_id=10,
        judge_fn=lambda **kw: _mock_verdict(winner_side="b", confidence=8.0, winner="Vegeta"),
        allow_receipt_refresh=False,
    )
    outcomes = list(iter_guild_outcomes(db, guild_id=1))
    # One fight outcome; winner is B's advocate (20).
    assert len(outcomes) == 1
    oc = outcomes[0]
    assert oc.winner_id == 20
    assert oc.loser_id == 10
    db.close()


def test_drop_reconsideration_messages_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from bot.commands import drop_reconsideration_messages, RECONSIDERATION_EMBED_TITLE

    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _ruled_fight(db)
    fid = int(fight["id"])
    result = reconsider_fight(
        db,
        fid,
        evidence="Drop test evidence.",
        now=FROZEN + timedelta(hours=1),
        actor_id=10,
        judge_fn=lambda **kw: _mock_verdict(winner_side="b", confidence=5.0),
        allow_receipt_refresh=False,
    )

    thread = MagicMock()
    thread.id = 9001
    thread_msg = MagicMock()
    thread_msg.id = 555
    thread.send = AsyncMock(return_value=thread_msg)

    async def _run() -> None:
        await drop_reconsideration_messages(thread=thread, result=result)

    asyncio.run(_run())
    thread.send.assert_awaited()
    embed = thread.send.await_args.kwargs.get("embed")
    assert embed is not None
    assert embed.title == RECONSIDERATION_EMBED_TITLE
    assert embed.title == "Ruling on reconsideration"
    diff_vals = [f.value for f in embed.fields if f.name == "Diff"]
    assert diff_vals and diff_vals[0] == "Winner reversed"
    row = db.get_ruling_by_id(int(result["ruling_id"]))
    assert row is not None
    assert row["message_id"] == 555
    db.close()
