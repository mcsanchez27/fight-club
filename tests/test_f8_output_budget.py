"""F8 — the ruling must not be truncated, and truncation must not look like a bug.

`deliver_verdict` is long-form: two steelmans, then the ruling, with citations
LAST in VERDICT_FIELD_ORDER. At max_tokens=2048 a real Goku vs Superman call
returned stop_reason=max_tokens at exactly 2048 out — sometimes losing only the
tail (citations, argument_quality) and sometimes `ruling` itself, which surfaced
to the user as the misleading "Verdict missing required field: ruling".

Verified live: 2048 → stop_reason=max_tokens, citations dropped.
                4096 → stop_reason=tool_use, 1966 out, citations present.

Because the natural length sits just under 2048, this was intermittent — which
is why it survived a full V1 cycle.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from bot.judge import (
    RULING_MAX_OUTPUT_TOKENS_DEFAULT,
    VERDICT_CORE_REQUIRED,
    _extract_tool_verdict,
    ruling_max_output_tokens,
)


def _complete_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "steelman_a": "a",
        "steelman_b": "b",
        "exhibit_ledger": [],
        "concessions": [],
        "unknowns": [],
        "ruling": "ruled on the record",
        "winner_side": "A",
        "confidence": 6,
        "citations": [],
    }
    payload.update(overrides)
    return payload


def _resp(payload: dict[str, Any], stop_reason: str) -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name="deliver_verdict", input=payload)
    return SimpleNamespace(content=[block], stop_reason=stop_reason)


# --- budget ------------------------------------------------------------------


def test_default_budget_clears_the_observed_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real verdict measured 1966 output tokens — 2048 was too tight."""
    monkeypatch.delenv("FIGHT_MAX_OUTPUT_TOKENS", raising=False)
    assert ruling_max_output_tokens() == RULING_MAX_OUTPUT_TOKENS_DEFAULT == 4096
    assert ruling_max_output_tokens() > 2048


def test_budget_is_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIGHT_MAX_OUTPUT_TOKENS", "8192")
    assert ruling_max_output_tokens() == 8192


@pytest.mark.parametrize("raw", ["", "   ", "banana", "not-a-number"])
def test_junk_budget_falls_back_to_default(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIGHT_MAX_OUTPUT_TOKENS", raw)
    assert ruling_max_output_tokens() == RULING_MAX_OUTPUT_TOKENS_DEFAULT


@pytest.mark.parametrize("raw", ["1", "0", "-500", "512"])
def test_budget_has_a_floor(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A verdict cannot physically fit below this, so don't let config break it."""
    monkeypatch.setenv("FIGHT_MAX_OUTPUT_TOKENS", raw)
    assert ruling_max_output_tokens() == 1024


# --- truncation handling -----------------------------------------------------


def test_complete_verdict_passes_through() -> None:
    verdict = _extract_tool_verdict(_resp(_complete_payload(), "tool_use"))
    assert verdict["ruling"] == "ruled on the record"


def test_truncated_and_missing_required_says_so_plainly() -> None:
    """The old message blamed the verdict; this one names the real cause."""
    payload = _complete_payload()
    del payload["ruling"]

    with pytest.raises(ValueError) as exc:
        _extract_tool_verdict(_resp(payload, "max_tokens"))

    msg = str(exc.value)
    assert "cut off" in msg
    assert "ruling" in msg
    assert "FIGHT_MAX_OUTPUT_TOKENS" in msg


def test_missing_field_without_truncation_keeps_the_original_error() -> None:
    """Not every missing field is truncation — don't mislabel a real one."""
    payload = _complete_payload()
    del payload["ruling"]

    with pytest.raises(ValueError) as exc:
        _extract_tool_verdict(_resp(payload, "tool_use"))

    assert "missing required field" in str(exc.value)
    assert "cut off" not in str(exc.value)


def test_truncated_but_usable_warns_instead_of_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Losing citations shouldn't kill a rulable verdict, but must not be silent."""
    payload = _complete_payload()
    payload.pop("citations")

    with caplog.at_level(logging.WARNING, logger="bot.judge"):
        verdict = _extract_tool_verdict(_resp(payload, "max_tokens"))

    assert verdict["ruling"] == "ruled on the record"
    assert any("truncated" in r.message.lower() for r in caplog.records), (
        "a silently receipt-less ruling is the failure mode this guards"
    )


def test_every_core_required_field_is_reported_when_cut_off() -> None:
    payload = {"steelman_a": "a", "steelman_b": "b"}

    with pytest.raises(ValueError) as exc:
        _extract_tool_verdict(_resp(payload, "max_tokens"))

    msg = str(exc.value)
    for field in VERDICT_CORE_REQUIRED:
        if field not in payload:
            assert field in msg, f"{field} not named in the error"
