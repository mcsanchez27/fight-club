"""Verdict schema validation (steelman-first field set)."""

import pytest

from bot.judge import VERDICT_REQUIRED_FIELDS, validate_verdict


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
    # ruling/winner/confidence/citations come after steelman/concessions/unknowns
    order = list(VERDICT_REQUIRED_FIELDS)
    assert order.index("steelman_a") < order.index("ruling")
    assert order.index("steelman_b") < order.index("ruling")
    assert order.index("concessions") < order.index("ruling")
    assert order.index("unknowns") < order.index("ruling")
    assert order.index("ruling") < order.index("winner")
    assert order.index("winner") < order.index("confidence")
    assert order.index("confidence") < order.index("citations")


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


def test_house_rule_6_is_migraine() -> None:
    from bot.judge import SYSTEM_PROMPT

    assert "The migraine gets the final say" in SYSTEM_PROMPT
    assert "Transparent confidence beats fake neutrality" not in SYSTEM_PROMPT
