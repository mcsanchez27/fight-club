"""V2 item 3 — prompt templates, balance_read tool, franchise normalize, verdict soft schema."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bot.judge import (
    BALANCE_READ_TOOL,
    DELIVER_VERDICT_TOOL,
    NULLABLE_SCORE_FIELDS,
    VERDICT_FIELD_ORDER,
    VERDICT_TOOL_REQUIRED_FIELDS,
    balance_model,
    balance_read,
    build_system_prompt,
    validate_balance,
    validate_verdict,
)
from bot.prompts import (
    PromptTemplateError,
    load_prompt,
    render_balance_prompt,
    render_referee_prompt,
)
from bot.sources import normalize_franchise_key


def test_prompt_templates_load_from_disk() -> None:
    referee = load_prompt("referee.md")
    balance = load_prompt("balance.md")
    assert "{{HOUSE_RULES}}" in referee or "HOUSE RULES" in referee
    assert "{{LAWS}}" in referee
    assert "deliver_verdict" in referee
    assert "{{MATCHUP}}" in balance
    assert "balance_read" in balance


def test_missing_prompt_template_errors_clearly(tmp_path: Path) -> None:
    with pytest.raises(PromptTemplateError, match="missing"):
        load_prompt("nope.md", prompts_dir=tmp_path)


def test_render_referee_injects_house_rules_and_laws() -> None:
    text = render_referee_prompt(laws="- **The Maki Law:** test")
    assert "The migraine gets the final say" in text
    assert "The Maki Law" in text
    assert "Steelman first" in text


def test_build_system_prompt_uses_disk_template() -> None:
    from bot.laws import load_laws

    load_laws()
    prompt = build_system_prompt()
    assert "Fight Club Court" in prompt
    assert "The migraine gets the final say" in prompt
    assert "The Maki Law" in prompt
    # Must not be the old fully-inline hardcode-only path without template markers resolved
    assert "{{HOUSE_RULES}}" not in prompt
    assert "{{LAWS}}" not in prompt


def test_balance_tool_schema_exact_fields() -> None:
    assert BALANCE_READ_TOOL["name"] == "balance_read"
    schema = BALANCE_READ_TOOL["input_schema"]
    assert list(schema["properties"]) == [
        "score",
        "favored_side",
        "reason",
        "franchise_a",
        "franchise_b",
    ]
    assert schema["required"] == [
        "score",
        "favored_side",
        "reason",
        "franchise_a",
        "franchise_b",
    ]
    assert schema["properties"]["favored_side"]["enum"] == ["a", "b", "even"]


def test_normalize_franchise_key_mapped_and_unmapped() -> None:
    assert normalize_franchise_key("Dragon Ball") == "dragon_ball"
    assert normalize_franchise_key("dbz") == "dragon_ball"
    assert normalize_franchise_key("Lord of the Rings") == "lotr"
    assert normalize_franchise_key("lotr_films") is None
    assert normalize_franchise_key("") is None
    assert normalize_franchise_key(None) is None


def test_validate_balance_normalizes_franchises() -> None:
    mapped = validate_balance(
        {
            "score": 8,
            "favored_side": "a",
            "reason": "Goku outscales hard",
            "franchise_a": "Dragon Ball",
            "franchise_b": "made_up_verse",
        }
    )
    assert mapped["franchise_a"] == "dragon_ball"
    assert mapped["franchise_b"] is None
    assert mapped["franchise_a_unlisted"] is False
    assert mapped["franchise_b_unlisted"] is True
    assert mapped["franchise_b_raw"] == "made_up_verse"

    even = validate_balance(
        {
            "score": 10,
            "favored_side": "even",
            "reason": "peer duel",
            "franchise_a": "asoiaf",
            "franchise_b": "A Song of Ice and Fire",
        }
    )
    assert even["franchise_a"] == "asoiaf"
    assert even["franchise_b"] == "asoiaf"
    assert even["favored_side"] == "even"


def test_validate_balance_caps_reason_words() -> None:
    words = " ".join(f"w{i}" for i in range(40))
    out = validate_balance(
        {
            "score": 5,
            "favored_side": "b",
            "reason": words,
            "franchise_a": "",
            "franchise_b": "lotr",
        }
    )
    assert len(out["reason"].split()) == 25
    assert out["franchise_a"] is None
    assert out["franchise_a_unlisted"] is True
    assert out["franchise_b"] == "lotr"


def test_deliver_verdict_field_order_steelman_before_ruling() -> None:
    order = list(VERDICT_FIELD_ORDER)
    assert order.index("steelman_a") < order.index("ruling")
    assert order.index("steelman_b") < order.index("ruling")
    assert order.index("exhibit_ledger") < order.index("ruling")
    assert order.index("concessions") < order.index("ruling")
    assert order.index("unknowns") < order.index("ruling")
    assert order.index("ruling") < order.index("winner_side")
    assert order.index("winner_side") < order.index("confidence")
    assert order.index("confidence") < order.index("argument_quality")
    assert order.index("argument_quality") < order.index("citations")

    schema = DELIVER_VERDICT_TOOL["input_schema"]
    props = list(schema["properties"])
    # First N properties follow V2 order (optional V1 keys may trail).
    for i, key in enumerate(VERDICT_FIELD_ORDER):
        assert props[i] == key
    assert schema["required"] == list(VERDICT_TOOL_REQUIRED_FIELDS)
    for score in NULLABLE_SCORE_FIELDS:
        assert score not in schema["required"]
        assert score in schema["properties"]


def test_nullable_capture_scores_do_not_fail() -> None:
    v = validate_verdict(
        {
            "steelman_a": "A strong open",
            "steelman_b": "B clever",
            "exhibit_ledger": [],
            "concessions": [],
            "unknowns": [],
            "ruling": "A takes it.",
            "winner_side": "a",
            "confidence": 6.5,
            "citations": [],
            # opening/close/argument_quality intentionally omitted
        }
    )
    assert v["winner_side"] == "a"
    assert v["winner"] == "A"
    assert v["opening_score_a"] is None
    assert v["opening_score_b"] is None
    assert v["close_score_a"] is None
    assert v["close_score_b"] is None
    assert v["argument_quality"] is None
    assert v["confidence"] == 6.5


def test_v1_verdict_still_validates() -> None:
    v = validate_verdict(
        {
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
    )
    assert v["winner"] == "A"
    assert v["exhibit_ledger"] == []
    assert v["citations"][0]["claim"] == "canon ch.1"


def test_balance_read_helper_mockable() -> None:
    block = MagicMock()
    block.type = "tool_use"
    block.name = "balance_read"
    block.input = {
        "score": 7,
        "favored_side": "b",
        "reason": "Prep and gadgets edge it",
        "franchise_a": "dragon ball",
        "franchise_b": "Unknown Comics",
    }
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 40
    resp = MagicMock()
    resp.content = [block]
    resp.usage = usage
    client = MagicMock()
    client.messages.create.return_value = resp

    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        result = balance_read("Goku", "Batman", context="no prep", client=client)

    assert result["score"] == 7.0
    assert result["favored_side"] == "b"
    assert result["franchise_a"] == "dragon_ball"
    assert result["franchise_b"] is None
    assert result["franchise_b_unlisted"] is True
    assert result["_usage"]["role"] == "balance"
    assert result["_usage"]["input_tokens"] == 100
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["model"] == balance_model()
    assert kwargs["tools"][0]["name"] == "balance_read"
    assert kwargs["tool_choice"]["name"] == "balance_read"
    assert "Goku vs Batman" in kwargs["messages"][0]["content"]


def test_render_balance_prompt_includes_matchup() -> None:
    text = render_balance_prompt(matchup="Aragorn vs Goku", context="open field")
    assert "Aragorn vs Goku" in text
    assert "open field" in text
