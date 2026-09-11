"""Verdict schema validation (steelman-first field set) and tool-use path."""

from unittest.mock import MagicMock, patch

import pytest

from bot.judge import (
    DELIVER_VERDICT_TOOL,
    VERDICT_REQUIRED_FIELDS,
    _extract_tool_verdict,
    _judge_anthropic,
    validate_verdict,
)


def _valid(**overrides):
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


def test_required_fields_are_steelman_first() -> None:
    order = list(VERDICT_REQUIRED_FIELDS)
    assert order.index("steelman_a") < order.index("ruling")
    assert order.index("steelman_b") < order.index("ruling")
    assert order.index("concessions") < order.index("ruling")
    assert order.index("unknowns") < order.index("ruling")
    assert order.index("ruling") < order.index("winner")
    assert order.index("winner") < order.index("confidence")
    assert order.index("confidence") < order.index("citations")


def test_deliver_verdict_tool_schema_matches_required() -> None:
    assert DELIVER_VERDICT_TOOL["name"] == "deliver_verdict"
    schema = DELIVER_VERDICT_TOOL["input_schema"]
    assert schema["required"] == list(VERDICT_REQUIRED_FIELDS)
    for key in VERDICT_REQUIRED_FIELDS:
        assert key in schema["properties"]


def test_validate_verdict_ok() -> None:
    v = validate_verdict(_valid())
    assert v["confidence"] == 7.0
    assert v["winner"] == "A"


def test_validate_verdict_coerces_list_fields() -> None:
    v = validate_verdict(_valid(citations="solo cite", concessions="one", unknowns="?"))
    assert v["citations"] == ["solo cite"]
    assert v["concessions"] == ["one"]
    assert v["unknowns"] == ["?"]


def test_validate_verdict_missing_field() -> None:
    bad = _valid()
    del bad["steelman_a"]
    with pytest.raises(ValueError, match="steelman_a"):
        validate_verdict(bad)


def test_extract_tool_verdict() -> None:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "deliver_verdict"
    block.input = _valid()
    resp = MagicMock()
    resp.content = [block]
    assert _extract_tool_verdict(resp)["winner"] == "A"


def test_anthropic_retries_once_on_validation_failure() -> None:
    good_block = MagicMock()
    good_block.type = "tool_use"
    good_block.name = "deliver_verdict"
    good_block.input = _valid()

    bad_block = MagicMock()
    bad_block.type = "tool_use"
    bad_block.name = "deliver_verdict"
    bad_block.input = {"matchup": "only"}  # missing fields

    bad_resp = MagicMock()
    bad_resp.content = [bad_block]
    good_resp = MagicMock()
    good_resp.content = [good_block]

    client = MagicMock()
    client.messages.create.side_effect = [bad_resp, good_resp]

    with patch("anthropic.Anthropic", return_value=client), patch.dict(
        "os.environ", {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_MODEL": "claude-test"}
    ):
        result = _judge_anthropic("Matchup: A vs B")

    assert result["winner"] == "A"
    assert client.messages.create.call_count == 2
    # tool_choice forced
    kwargs = client.messages.create.call_args_list[0].kwargs
    assert kwargs["tools"][0]["name"] == "deliver_verdict"
    assert kwargs["tool_choice"]["name"] == "deliver_verdict"


def test_house_rule_6_is_migraine() -> None:
    from bot.judge import build_system_prompt
    from bot.laws import load_laws

    load_laws()
    prompt = build_system_prompt()
    assert "The migraine gets the final say" in prompt
    assert "Transparent confidence beats fake neutrality" not in prompt
    assert "The Maki Law" in prompt


def test_no_code_fence_stripping_helper() -> None:
    import bot.judge as j

    assert not hasattr(j, "_parse_verdict")
