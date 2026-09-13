"""V2 item 5 — thread on Accept + receipts keyed by fight."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.db import CourtDB
from bot.fights import (
    accept_fight,
    create_proposed_fight,
    fetch_and_store_accept_receipts,
    format_opening_message,
    to_iso,
)
from bot.progress import PROGRESS_RETRIEVING, RECEIPTS_GAP_NOTE
from bot.retrieval import (
    RETRIEVAL_BUDGET_SECONDS,
    Passage,
    RetrievalResult,
    clear_retrieval_cache,
    retrieve_for_accept,
)


FROZEN = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)


def _db(tmp_path: Path) -> CourtDB:
    return CourtDB(tmp_path / "court.db")


def _proposed(db: CourtDB, **overrides):
    kw = dict(
        guild_id=1,
        channel_id=2,
        challenger_id=10,
        challengee_id=20,
        side_a="Goku",
        side_b="Vegeta",
        context="open field",
        now=FROZEN,
    )
    kw.update(overrides)
    fight = create_proposed_fight(db, **kw)
    # Simulate balance franchises already stored.
    db.update_fight(
        int(fight["id"]),
        franchise_a="dragon_ball",
        franchise_b="dragon_ball",
    )
    return db.get_fight(int(fight["id"]))


def test_opening_message_helper_content() -> None:
    text = format_opening_message(
        {
            "side_a": "Aragorn",
            "side_b": "Goku",
            "context": "no ki",
            "advocate_a_id": 11,
            "advocate_b_id": 22,
        }
    )
    assert "Aragorn vs Goku" in text
    assert "Side A" in text and "Aragorn" in text
    assert "Side B" in text and "Goku" in text
    assert "no ki" in text
    assert "/rest" in text
    assert "<@11>" in text and "<@22>" in text


def test_accept_creates_thread_id_arguing_and_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = _db(tmp_path)
    monkeypatch.setattr("bot.commands.get_db", lambda: db)
    fight = _proposed(db)

    from bot.commands import ChallengeCardView

    view = ChallengeCardView(int(fight["id"]))
    thread = MagicMock(id=901)
    thread.send = AsyncMock()

    interaction = MagicMock()
    interaction.user.id = 20
    interaction.message = MagicMock()
    interaction.message.create_thread = AsyncMock(return_value=thread)
    interaction.response = MagicMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.edit_message = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    empty = RetrievalResult(
        franchise="dragon_ball", status="unavailable", receipts=[], exhibits=[]
    )

    async def _run() -> None:
        with patch("bot.commands.utc_now", return_value=FROZEN), patch(
            "bot.commands.fetch_and_store_accept_receipts", return_value=empty
        ), patch("bot.commands.apply_balance_to_fight", return_value=None):
            await view.on_accept(interaction)

    asyncio.run(_run())

    interaction.message.create_thread.assert_awaited()
    thread.send.assert_awaited()
    opening = thread.send.await_args.args[0]
    assert "Goku vs Vegeta" in opening
    assert "/rest" in opening

    row = db.get_fight(fight["id"])
    assert row["status"] == "arguing"
    assert row["accepted_at"] == to_iso(FROZEN)
    assert row["thread_id"] == 901
    # Progress + final edits
    assert interaction.response.edit_message.await_count >= 1
    final_contents = [
        c.kwargs.get("content") or (c.args[0] if c.args else "")
        for c in interaction.response.edit_message.await_args_list
    ]
    assert any(PROGRESS_RETRIEVING in str(c) for c in final_contents) or any(
        RECEIPTS_GAP_NOTE in str(c) for c in final_contents
    )
    db.close()


def test_receipts_stored_keyed_by_fight_accept_succeeds_when_empty(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    fight = _proposed(db)
    fid = int(fight["id"])

    passages = [
        Passage(
            claim="Goku power",
            source_url="https://dragonball.fandom.com/wiki/Goku",
            locator="Power",
            snippet="word " * 10,
            verified=True,
            retrieved_at="2026-09-13T12:00:00Z",
            retrieval_id="ret_1",
        )
    ]

    def fake_retrieve(*_a, **_k):
        return RetrievalResult(
            franchise="dragon_ball",
            status="ok",
            receipts=passages,
            exhibits=[],
            retrieval_seconds=0.01,
        )

    result = fetch_and_store_accept_receipts(
        db, fid, retrieve_fn=fake_retrieve, budget_seconds=10
    )
    assert result.status == "ok"
    stored = db.list_receipts(fid)
    assert len(stored) == 1
    assert stored[0]["fight_id"] == fid
    assert stored[0]["source_url"].endswith("/Goku")
    assert stored[0]["verified"] is True
    assert db.get_fight(fid)["retrieval_status"] == "ok"

    # Empty / unavailable — Accept still transitions.
    def empty_retrieve(*_a, **_k):
        return RetrievalResult(
            franchise="dragon_ball",
            status="unavailable",
            receipts=[],
            exhibits=[],
        )

    result2 = fetch_and_store_accept_receipts(
        db, fid, retrieve_fn=empty_retrieve, budget_seconds=10
    )
    assert result2.status == "unavailable"
    assert db.list_receipts(fid) == []
    assert db.get_fight(fid)["retrieval_status"] == "unavailable"

    out = accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=42)
    assert out["status"] == "arguing"
    assert out["thread_id"] == 42
    assert out["retrieval_status"] == "unavailable"
    # No gallery exhibits created.
    assert db.list_exhibits(fid) == []
    db.close()


def test_accept_succeeds_when_retrieve_fn_raises(tmp_path: Path) -> None:
    db = _db(tmp_path)
    fight = _proposed(db)
    fid = int(fight["id"])

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    result = fetch_and_store_accept_receipts(db, fid, retrieve_fn=boom)
    assert result.status == "unavailable"
    assert db.get_fight(fid)["retrieval_status"] == "unavailable"
    out = accept_fight(db, fid, now=FROZEN, actor_id=20, thread_id=1)
    assert out["status"] == "arguing"
    db.close()


def test_retrieve_for_accept_respects_budget_mocked_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared 10s budget: second franchise call sees exhausted remaining time."""
    clear_retrieval_cache()
    calls: list[float] = []

    def fake_retrieve(*_a, **kwargs):
        budget = float(kwargs.get("budget_seconds") or 0)
        calls.append(budget)
        time.sleep(0.05)
        return RetrievalResult(
            franchise=kwargs.get("franchise_hint"),
            status="unavailable",
            receipts=[],
            exhibits=[],
            retrieval_seconds=0.05,
        )

    # Force two distinct franchises so retrieve_for_accept splits the budget.
    with patch("bot.retrieval.retrieve", side_effect=fake_retrieve), patch(
        "bot.retrieval.retrieval_enabled", return_value=True
    ):
        result = retrieve_for_accept(
            "Aragorn",
            "Goku",
            "field",
            franchise_a="lotr",
            franchise_b="dragon_ball",
            budget_seconds=0.08,
        )

    assert len(calls) >= 1
    assert all(c <= 0.08 + 1e-6 for c in calls)
    assert result.retrieval_seconds <= 0.5
    assert RETRIEVAL_BUDGET_SECONDS == 10.0


def test_retrieve_for_accept_unlisted_when_no_franchises() -> None:
    clear_retrieval_cache()
    result = retrieve_for_accept(
        "A", "B", None, franchise_a=None, franchise_b=None, budget_seconds=10
    )
    assert result.status == "unlisted"
    assert result.receipts == []


def test_receipts_table_exists_on_fresh_db(tmp_path: Path) -> None:
    db = _db(tmp_path)
    names = {
        r[0]
        for r in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "receipts" in names
    cols = db._table_columns("receipts")
    assert {
        "id",
        "fight_id",
        "claim",
        "source_url",
        "locator",
        "snippet",
        "verified",
        "retrieved_at",
        "kind",
    } <= cols
    db.close()
