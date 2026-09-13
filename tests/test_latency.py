"""V2 item 1 — latency: retrieval budget/parallel, Anthropic client, tokens, progress."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.budget import input_usd_per_mtok, output_usd_per_mtok, record_estimated_usage, usage_from_verdict
from bot.db import CourtDB
from bot.judge import (
    ANTHROPIC_MAX_RETRIES,
    ANTHROPIC_TIMEOUT_SECONDS,
    DEFAULT_MODEL_BALANCE,
    DEFAULT_MODEL_RULING,
    _judge_anthropic,
    _usage_from_response,
    balance_model,
    make_anthropic_client,
    ruling_model,
)
from bot.progress import (
    PROGRESS_JUDGING,
    PROGRESS_RETRIEVING,
    edit_deferred_progress,
)
from bot.retrieval import (
    RETRIEVAL_BUDGET_SECONDS,
    Passage,
    clear_retrieval_cache,
    retrieve,
)


def _valid_verdict(**overrides):
    base = {
        "matchup": "A vs B",
        "steelman_a": "A is strong",
        "steelman_b": "B is clever",
        "concessions": ["A has speed"],
        "unknowns": [],
        "ruling": "A wins on speed.",
        "winner": "A",
        "confidence": 7,
        "citations": ["canon ch.1"],
    }
    base.update(overrides)
    return base


def test_retrieval_budget_constant_default() -> None:
    assert RETRIEVAL_BUDGET_SECONDS == 10.0


def test_retrieval_parallel_and_budget_cuts_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Independent source fetches overlap; wall clock stays near the budget."""
    clear_retrieval_cache()
    started: list[float] = []
    released = {"n": 0}

    def slow_fetch(franchise_key, query, src, *, deadline=None, cfg=None):
        started.append(time.monotonic())
        # Simulate a slow urllib that would blow past the budget if sequential.
        time.sleep(0.35)
        released["n"] += 1
        return [
            Passage(
                claim=f"{query}-{src.get('name')}",
                source_url=f"https://example.test/{query}",
                locator=str(src.get("name") or "src"),
                snippet="word " * 20,
                verified=True,
                retrieved_at="2026-09-13T00:00:00Z",
            )
        ]

    sources = [
        {"name": "S1", "base_url": "https://a.example", "api": "mediawiki"},
        {"name": "S2", "base_url": "https://b.example", "api": "mediawiki"},
        {"name": "S3", "base_url": "https://c.example", "api": "mediawiki"},
    ]

    with patch("bot.retrieval.franchise_sources", return_value=sources), patch(
        "bot.retrieval.detect_franchise", return_value="dragon_ball"
    ), patch("bot.retrieval._fetch_one_source", side_effect=slow_fetch), patch(
        "bot.retrieval.retrieval_enabled", return_value=True
    ):
        t0 = time.monotonic()
        result = retrieve("Goku", "Vegeta", budget_seconds=0.5)
        elapsed = time.monotonic() - t0

    # Wall clock should be well under sequential 3*2*0.35 (~2.1s).
    assert elapsed < 1.2
    assert result.retrieval_seconds <= 0.9
    # At least two workers overlapped near the start.
    assert len(started) >= 2
    assert max(started) - min(started) < 0.25
    assert result.status in {"ok", "unavailable"}


def test_retrieval_respects_zero_budget() -> None:
    clear_retrieval_cache()
    with patch("bot.retrieval.detect_franchise", return_value="dragon_ball"), patch(
        "bot.retrieval.retrieval_enabled", return_value=True
    ), patch("bot.retrieval._fetch_one_source") as fetch:
        result = retrieve("Goku", "Vegeta", budget_seconds=0)
    fetch.assert_not_called()
    assert result.status == "unavailable"
    assert result.receipts == []
    assert result.retrieval_seconds >= 0.0


def test_anthropic_client_timeout_and_retries() -> None:
    with patch("anthropic.Anthropic") as ctor:
        ctor.return_value = MagicMock()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            make_anthropic_client()
    kwargs = ctor.call_args.kwargs
    assert kwargs["timeout"] == ANTHROPIC_TIMEOUT_SECONDS == 60
    assert kwargs["max_retries"] == ANTHROPIC_MAX_RETRIES == 1


def test_ruling_and_balance_model_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIGHT_MODEL_RULING", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("FIGHT_MODEL_BALANCE", raising=False)
    assert ruling_model() == DEFAULT_MODEL_RULING == "claude-sonnet-5"
    assert balance_model() == DEFAULT_MODEL_BALANCE == "claude-haiku-4-5-20251001"
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-from-v1")
    assert ruling_model() == "claude-from-v1"
    monkeypatch.setenv("FIGHT_MODEL_RULING", "claude-override")
    assert ruling_model() == "claude-override"


def test_usage_from_response_reads_real_tokens() -> None:
    usage = MagicMock()
    usage.input_tokens = 1234
    usage.output_tokens = 56
    resp = MagicMock()
    resp.usage = usage
    assert _usage_from_response(resp) == {"input_tokens": 1234, "output_tokens": 56}


def test_judge_anthropic_books_real_tokens_and_client_settings() -> None:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "deliver_verdict"
    block.input = _valid_verdict()
    usage = MagicMock()
    usage.input_tokens = 999
    usage.output_tokens = 111
    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage

    client = MagicMock()
    client.messages.create.return_value = resp

    with patch("bot.judge.make_anthropic_client", return_value=client), patch.dict(
        "os.environ",
        {"ANTHROPIC_API_KEY": "test-key", "FIGHT_MODEL_RULING": "claude-sonnet-5"},
    ):
        result = _judge_anthropic("Matchup: A vs B")

    assert result["winner"] == "A"
    assert result["_usage"]["input_tokens"] == 999
    assert result["_usage"]["output_tokens"] == 111
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["tools"][0]["name"] == "deliver_verdict"
    assert kwargs["tools"][0].get("type") == "custom"
    assert kwargs["tool_choice"]["name"] == "deliver_verdict"


def test_record_usage_persists_stage_timings(tmp_path: Path) -> None:
    db = CourtDB(tmp_path / "court.db")
    record_estimated_usage(
        tokens_in=999,
        tokens_out=111,
        db=db,
        retrieval_seconds=1.25,
        judge_seconds=2.5,
        total_seconds=3.75,
    )
    row = db._conn.execute(
        "SELECT tokens_in, tokens_out, retrieval_seconds, judge_seconds, total_seconds "
        "FROM usage_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["tokens_in"] == 999
    assert row["tokens_out"] == 111
    assert row["retrieval_seconds"] == pytest.approx(1.25)
    assert row["judge_seconds"] == pytest.approx(2.5)
    assert row["total_seconds"] == pytest.approx(3.75)
    db.close()


def test_usage_from_verdict_helper() -> None:
    u = usage_from_verdict(
        {
            "_usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "retrieval_seconds": 1.0,
                "judge_seconds": 2.0,
                "total_seconds": 3.0,
            }
        }
    )
    assert u["tokens_in"] == 10
    assert u["tokens_out"] == 20
    assert u["total_seconds"] == 3.0


def test_sonnet5_pricing_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FIGHT_USD_PER_MTOK_INPUT", raising=False)
    monkeypatch.delenv("FIGHT_USD_PER_MTOK_OUTPUT", raising=False)
    assert input_usd_per_mtok() == 2.0
    assert output_usd_per_mtok() == 10.0


def test_progress_edit_helper_without_discord_loop() -> None:
    class FakeInteraction:
        def __init__(self) -> None:
            self.edits: list[str] = []

        async def edit_original_response(self, *, content: str) -> None:
            self.edits.append(content)

    ix = FakeInteraction()

    async def _run() -> None:
        ok1 = await edit_deferred_progress(ix, PROGRESS_RETRIEVING)
        ok2 = await edit_deferred_progress(ix, PROGRESS_JUDGING)
        assert ok1 and ok2

    asyncio.run(_run())
    assert ix.edits == ["Retrieving receipts…", "Judging…"]
