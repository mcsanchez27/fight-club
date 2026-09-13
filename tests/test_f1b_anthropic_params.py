"""F1b — Anthropic messages.create must omit temperature/top_p/top_k."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from bot.judge import _judge_anthropic, balance_read, judge_with_materials


_FORBIDDEN = ("temperature", "top_p", "top_k")


def _assert_no_sampling(kwargs: dict) -> None:
    for key in _FORBIDDEN:
        assert key not in kwargs, f"Anthropic kwargs must omit {key}, got {kwargs.get(key)!r}"


def _verdict_resp() -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "deliver_verdict"
    block.input = {
        "matchup": "A vs B",
        "winner": "A",
        "winner_side": "a",
        "confidence": 70,
        "steelman_a": "A argument",
        "steelman_b": "B argument",
        "ruling": "A wins on the record.",
        "key_exhibits": [],
    }
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    return resp


def _balance_resp() -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "balance_read"
    block.input = {
        "score": 5,
        "favored_side": "even",
        "reason": "Even matchup",
        "franchise_a": "dragon ball",
        "franchise_b": "dragon ball",
    }
    resp = MagicMock()
    resp.content = [block]
    resp.usage = MagicMock(input_tokens=8, output_tokens=4)
    return resp


def test_judge_anthropic_omits_sampling_params() -> None:
    client = MagicMock()
    client.messages.create.return_value = _verdict_resp()
    with patch("anthropic.Anthropic", return_value=client), patch.dict(
        "os.environ", {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_MODEL": "claude-test"}
    ):
        _judge_anthropic("Matchup: A vs B")
    assert client.messages.create.call_count >= 1
    for call in client.messages.create.call_args_list:
        _assert_no_sampling(call.kwargs)


def test_balance_read_omits_sampling_params() -> None:
    client = MagicMock()
    client.messages.create.return_value = _balance_resp()
    balance_read("Goku", "Vegeta", client=client)
    assert client.messages.create.call_count >= 1
    for call in client.messages.create.call_args_list:
        _assert_no_sampling(call.kwargs)


def test_judge_with_materials_omits_sampling_params() -> None:
    client = MagicMock()
    client.messages.create.return_value = _verdict_resp()
    judge_with_materials(
        fight_setup="A vs B",
        receipts="(none)",
        transcript="[A] hi",
        exhibit_ledger="(none)",
        client=client,
    )
    assert client.messages.create.call_count >= 1
    for call in client.messages.create.call_args_list:
        _assert_no_sampling(call.kwargs)
