"""V2 item 8 — judge_ready → ruled, snapshot-only, thin banner, archive_due."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.embeds import ruling_drop_embed
from bot.fights import (
    JUDGE_READY_STATUS,
    RULED_STATUS,
    accept_fight,
    archive_due,
    create_proposed_fight,
    mark_judge_ready,
    rest_fight,
    sweep_deadlines,
    thread_archive_delay_hours,
    to_iso,
)
from bot.judge import validate_verdict
from bot.progress import PROGRESS_JUDGING
from bot.retrieval import Passage, RetrievalResult
from bot.ruling import (
    THIN_RECORD_BANNER,
    display_name,
    ensure_transcript_snapshot,
    format_winner_one_liner,
    is_thin_record,
    load_or_refresh_receipts,
    rule_fight,
    side_message_counts,
)
from bot.transcript import build_and_store_transcript_snapshot


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


def _judge_ready_with_snapshot(db: CourtDB, messages: list | None = None) -> dict:
    fight = _arguing(db)
    fid = int(fight["id"])
    rest_fight(db, fid, now=FROZEN, actor_id=10)
    rest_fight(db, fid, now=FROZEN + timedelta(minutes=1), actor_id=20)
    fight = db.get_fight(fid)
    assert fight is not None
    assert fight["status"] == JUDGE_READY_STATUS
    msgs = messages
    if msgs is None:
        msgs = [
            _msg(1, 10, "Goku opens with UI and stamina."),
            _msg(2, 20, "Vegeta answers with pride and galick."),
        ]
    build_and_store_transcript_snapshot(db, fight, msgs)
    # Seed a receipt so refresh is not required.
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
    return db.get_fight(fid)  # type: ignore[return-value]


# --- Helpers -----------------------------------------------------------------


def test_display_name_and_one_liner() -> None:
    assert display_name("Alice") == "Alice"
    user = MagicMock(display_name="Bob", name="bob", id=99)
    assert display_name(user) == "Bob"
    line = format_winner_one_liner(
        winner_name="Alice",
        side_a="Goku",
        side_b="Vegeta",
        jump_link="https://discord.com/channels/1/2/3",
    )
    assert line.startswith("🏆 **Alice** won *Goku vs Vegeta*")
    assert "https://discord.com/channels/1/2/3" in line


def test_ruling_drop_embed_content_and_thin_banner() -> None:
    v = _mock_verdict()
    fight = {"side_a": "Goku", "side_b": "Vegeta"}
    embed = ruling_drop_embed(
        v,
        fight=fight,
        thin_record=True,
        exhibit_ledger="E1: link https://x [verified]",
    )
    assert THIN_RECORD_BANNER in (embed.description or "")
    names = [f.name for f in embed.fields]
    assert "Steelman A" in names
    assert "Steelman B" in names
    assert "Concessions" in names
    assert "Exhibit ledger" in names
    assert "Verdict" in names  # V2.1: winner + confidence on one line
    assert "Citations" in names
    assert "Goku" in embed.fields[names.index("Verdict")].value


def test_nullable_scores_do_not_fail_validation() -> None:
    v = validate_verdict(
        {
            "steelman_a": "A",
            "steelman_b": "B",
            "ruling": "A wins",
            "winner_side": "a",
            "confidence": 6,
            "citations": [],
            # opening/close/argument_quality omitted on purpose
        }
    )
    assert v["opening_score_a"] is None
    assert v["close_score_b"] is None
    assert v["argument_quality"] is None
    assert v["winner_side"] == "a"


def test_thin_record_detection() -> None:
    snap_ok = {
        "turns": [
            {"side": "a", "content": "hi"},
            {"side": "b", "content": "yo"},
        ],
        "text": "[A:Goku] hi\n[B:Vegeta] yo",
    }
    assert is_thin_record(snap_ok) is False
    snap_thin = {
        "turns": [{"side": "a", "content": "solo"}],
        "text": "[A:Goku] solo",
    }
    assert is_thin_record(snap_thin) is True
    assert side_message_counts(snap_thin) == {"a": 1, "b": 0}
    assert is_thin_record(None) is True


# --- Core rule path ----------------------------------------------------------


def test_judge_ready_rules_once_snapshot_only(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _judge_ready_with_snapshot(db)
    fid = int(fight["id"])
    calls = {"n": 0}

    def fake_judge(**kwargs):
        calls["n"] += 1
        # Transcript must come from snapshot text, not a later live fetch.
        assert "Goku opens" in kwargs.get("transcript", "")
        assert "SHOULD NOT" not in kwargs.get("transcript", "")
        return _mock_verdict()

    result = rule_fight(
        db,
        fid,
        now=FROZEN + timedelta(minutes=5),
        messages=None,  # must use stored snapshot
        judge_fn=fake_judge,
        allow_receipt_refresh=False,
    )
    assert result["already_ruled"] is False
    assert result["fight"]["status"] == RULED_STATUS
    assert result["fight"]["ruled_at"]
    assert result["fight"]["archive_at"]
    assert result["ruling_id"]
    assert calls["n"] == 1

    ruling = db.get_ruling_by_id(int(result["ruling_id"]))
    assert ruling is not None
    assert ruling["kind"] == "initial"
    assert ruling["winner_side"] == "a"
    assert ruling["fight_id"] == fid
    assert ruling["transcript_snapshot"] is not None
    assert ruling["opening_score_a"] is None  # nullable persisted

    # Second call — idempotent, no second judge; poison messages ignored.
    poison = [_msg(99, 10, "SHOULD NOT BE USED AFTER STORE")]
    again = rule_fight(
        db,
        fid,
        now=FROZEN + timedelta(minutes=6),
        messages=poison,
        judge_fn=fake_judge,
    )
    assert again["already_ruled"] is True
    assert calls["n"] == 1
    # Snapshot unchanged (amendment 2 — no second live dependency after store).
    stored = db.get_fight(fid)
    assert stored is not None
    assert "SHOULD NOT" not in str(stored.get("transcript_snapshot"))
    assert "Goku opens" in stored["transcript_snapshot"]["text"]
    # ensure_transcript_snapshot also ignores new messages once stored.
    snap = ensure_transcript_snapshot(db, stored, poison)
    assert "SHOULD NOT" not in snap["text"]
    db.close()


def test_ensure_snapshot_builds_once_then_frozen(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _arguing(db)
    fid = int(fight["id"])
    mark_judge_ready(db, fid)
    fight = db.get_fight(fid)
    assert fight is not None
    assert fight.get("transcript_snapshot") is None

    msgs1 = [
        _msg(1, 10, "First A argument."),
        _msg(2, 20, "First B argument."),
    ]
    snap1 = ensure_transcript_snapshot(db, fight, msgs1)
    assert "First A" in snap1["text"]

    # Mutate messages / pass new ones — must NOT rebuild.
    msgs1[0]["content"] = "MUTATED"
    fight2 = db.get_fight(fid)
    assert fight2 is not None
    snap2 = ensure_transcript_snapshot(
        db,
        fight2,
        [_msg(3, 10, "late add should be ignored")],
    )
    assert snap2["text"] == snap1["text"]
    assert "MUTATED" not in snap2["text"]
    assert "late add" not in snap2["text"]
    db.close()


def test_thin_record_banner_when_missing_side(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Only side A spoke.
    fight = _judge_ready_with_snapshot(
        db, messages=[_msg(1, 10, "Only Goku argued today.")]
    )
    fid = int(fight["id"])

    result = rule_fight(
        db,
        fid,
        now=FROZEN,
        judge_fn=lambda **kw: _mock_verdict(),
        allow_receipt_refresh=False,
    )
    assert result["thin_record"] is True
    assert result["verdict"]["thin_record_banner"] == THIN_RECORD_BANNER
    embed = ruling_drop_embed(
        result["verdict"],
        fight=result["fight"],
        thin_record=True,
        exhibit_ledger=result["exhibit_ledger"],
    )
    assert THIN_RECORD_BANNER in (embed.description or "")
    db.close()


def test_receipt_refresh_once_when_empty(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _judge_ready_with_snapshot(db)
    fid = int(fight["id"])
    db.clear_fight_receipts(fid)
    db.update_fight(fid, retrieval_status="unavailable")
    fight = db.get_fight(fid)
    assert fight is not None

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

    receipts, status, did = load_or_refresh_receipts(
        db, fight, retrieve_fn=fake_retrieve, allow_refresh=True
    )
    assert did is True
    assert refreshes["n"] == 1
    assert status == "ok"
    assert any(r["claim"] == "refreshed" for r in receipts)

    # Second call — receipts present + ok → no refresh.
    fight2 = db.get_fight(fid)
    assert fight2 is not None
    _, _, did2 = load_or_refresh_receipts(
        db, fight2, retrieve_fn=fake_retrieve, allow_refresh=True
    )
    assert did2 is False
    assert refreshes["n"] == 1
    db.close()


def test_archive_due_frozen_clock(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _judge_ready_with_snapshot(db)
    fid = int(fight["id"])
    result = rule_fight(
        db,
        fid,
        now=FROZEN,
        judge_fn=lambda **kw: _mock_verdict(),
        allow_receipt_refresh=False,
    )
    assert result["fight"]["status"] == RULED_STATUS
    archive_at = result["archive_at"]
    assert archive_at == to_iso(FROZEN + timedelta(hours=24))

    archived: list[int] = []

    def arch(f: dict) -> None:
        archived.append(int(f["id"]))

    # Before deadline — nothing.
    assert archive_due(db, FROZEN + timedelta(hours=23), archive_thread=arch) == []
    assert archived == []

    # Past deadline — once.
    due = archive_due(
        db, FROZEN + timedelta(hours=24, seconds=1), archive_thread=arch
    )
    assert due == [fid]
    assert archived == [fid]
    # archive_at cleared — not due again.
    assert archive_due(
        db, FROZEN + timedelta(hours=48), archive_thread=arch
    ) == []
    assert archived == [fid]

    # sweep includes archived key
    out = sweep_deadlines(db, FROZEN + timedelta(hours=48), archive_thread=arch)
    assert "archived" in out
    db.close()


def test_thread_archive_delay_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.delenv("FIGHT_THREAD_ARCHIVE_DELAY_HOURS", raising=False)
    assert thread_archive_delay_hours(db, None) == 24.0
    monkeypatch.setenv("FIGHT_THREAD_ARCHIVE_DELAY_HOURS", "12")
    assert thread_archive_delay_hours(db, None) == 12.0
    db.set_guild_config(1, "thread_archive_delay_hours", "6")
    assert thread_archive_delay_hours(db, 1) == 6.0
    db.close()


def test_drop_ruling_messages_embed_and_one_liner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discord drop helper: thread embed + channel one-liner + message_id stamp."""
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _judge_ready_with_snapshot(db)
    fid = int(fight["id"])
    # Persist a real ruling row so update_ruling can stamp message_id.
    result = rule_fight(
        db,
        fid,
        now=FROZEN,
        judge_fn=lambda **kw: _mock_verdict(),
        allow_receipt_refresh=False,
    )

    from bot.commands import drop_ruling_messages

    thread = MagicMock()
    thread.id = 9001
    thread.send = AsyncMock(return_value=MagicMock(id=777))
    parent = MagicMock()
    parent.send = AsyncMock()

    async def _run() -> None:
        await drop_ruling_messages(
            thread=thread,
            parent_channel=parent,
            result=result,
            winner_display="Alice",
        )

    asyncio.run(_run())
    thread.send.assert_awaited()
    embed = thread.send.await_args.kwargs.get("embed") or thread.send.await_args.args[0]
    assert embed is not None
    parent.send.assert_awaited()
    one = parent.send.await_args.args[0]
    assert one.startswith("🏆 **Alice** won *Goku vs Vegeta*")
    assert "777" in one  # jump link includes message id
    ruling = db.get_ruling_by_id(int(result["ruling_id"]))
    assert ruling is not None
    assert ruling["message_id"] == 777
    db.close()


def test_progress_judging_constant() -> None:
    assert PROGRESS_JUDGING == "Judging…"


def test_mock_anthropic_judge_with_materials() -> None:
    """Mock Anthropic client delivers deliver_verdict tool payload."""
    from bot.judge import judge_with_materials

    block = MagicMock()
    block.type = "tool_use"
    block.name = "deliver_verdict"
    block.input = _mock_verdict()

    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=100, output_tokens=50)

    client = MagicMock()
    client.messages.create = MagicMock(return_value=resp)

    out = judge_with_materials(
        fight_setup="Goku vs Vegeta",
        receipts="(none)",
        transcript="[A:Goku] hi\n[B:Vegeta] yo",
        exhibit_ledger="(none)",
        client=client,
    )
    assert out["winner_side"] == "a"
    assert out["_usage"]["input_tokens"] == 100
    client.messages.create.assert_called()
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["tool_choice"]["name"] == "deliver_verdict"
    assert "Goku vs Vegeta" in kwargs["system"]
