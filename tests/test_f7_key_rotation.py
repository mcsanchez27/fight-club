"""F7 — a rotated API key recovers without restarting the bot.

Keys get cycled per dev session. The bot loads its environment once at startup,
so a key rotated mid-run surfaced as a raw 401 traceback inside the fight and
the only cure was a full restart.

Now an auth failure re-reads .env and retries once. If the key is still bad the
caller gets a plain-language RuntimeError instead of a provider stack trace.

These never touch the real .env: ``dotenv.load_dotenv`` is patched out and the
environment is monkeypatched per test.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock

import dotenv
import pytest

import bot.judge as judge

AUTH_401 = (
    "Error code: 401 - {'type': 'error', 'error': {'type': "
    "'authentication_error', 'message': 'API key is invalid.'}}"
)


# --- detection ---------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        Exception(AUTH_401),
        Exception("authentication_error"),
        Exception("API key is invalid."),
    ],
)
def test_auth_errors_are_recognised(exc: Exception) -> None:
    assert judge._is_auth_error(exc) is True


def test_status_code_401_is_recognised() -> None:
    exc = Exception("boom")
    exc.status_code = 401  # type: ignore[attr-defined]
    assert judge._is_auth_error(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        Exception("Error code: 529 - overloaded_error"),
        Exception("Connection timed out"),
        ValueError("missing deliver_verdict tool use"),
    ],
)
def test_other_errors_are_not_auth(exc: Exception) -> None:
    """A rate limit or timeout must not be mistaken for a bad key."""
    assert judge._is_auth_error(exc) is False


# --- reload ------------------------------------------------------------------


def test_reload_reports_a_changed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-old")
    monkeypatch.setattr(
        dotenv,
        "load_dotenv",
        lambda *a, **k: os.environ.__setitem__("ANTHROPIC_API_KEY", "sk-ant-new"),
    )
    assert judge.reload_api_key() is True
    assert os.getenv("ANTHROPIC_API_KEY") == "sk-ant-new"


def test_reload_reports_an_unchanged_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-same")
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    assert judge.reload_api_key() is False


def test_reload_survives_a_broken_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure reading .env must not replace the auth error with its own."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(
        dotenv, "load_dotenv", MagicMock(side_effect=OSError("unreadable"))
    )
    assert judge.reload_api_key() is False


# --- judge path --------------------------------------------------------------


def _wire(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(judge, "make_anthropic_client", lambda: client)
    monkeypatch.setattr(judge, "build_system_prompt", lambda: "system")
    monkeypatch.setattr(judge, "ruling_model", lambda: "claude-sonnet-5")
    monkeypatch.setattr(judge, "_extract_tool_verdict", lambda r: {"winner": "A"})
    monkeypatch.setattr(
        judge, "_usage_from_response", lambda r: {"input_tokens": 10, "output_tokens": 5}
    )


def _client_failing_first(n_failures: int) -> MagicMock:
    state = {"calls": 0}

    def create(**_: Any) -> str:
        state["calls"] += 1
        if state["calls"] <= n_failures:
            raise Exception(AUTH_401)
        return "response"

    client = MagicMock()
    client.messages.create.side_effect = create
    client.call_count_state = state  # type: ignore[attr-defined]
    return client


def test_rotated_key_recovers_without_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_failing_first(1)
    _wire(monkeypatch, client)
    monkeypatch.setattr(judge, "reload_api_key", lambda: True)

    verdict = judge._judge_anthropic("Goku vs Superman")

    assert verdict["winner"] == "A"
    assert client.call_count_state["calls"] == 2, "should retry exactly once"


def test_unchanged_key_fails_with_a_readable_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing to reload → don't burn a second call, just explain."""
    client = _client_failing_first(99)
    _wire(monkeypatch, client)
    monkeypatch.setattr(judge, "reload_api_key", lambda: False)

    with pytest.raises(RuntimeError) as exc:
        judge._judge_anthropic("Goku vs Superman")

    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert "401" not in str(exc.value), "provider noise should not reach the user"
    assert client.call_count_state["calls"] == 1, "no pointless retry"


def test_still_bad_after_reload_fails_readably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client_failing_first(99)
    _wire(monkeypatch, client)
    monkeypatch.setattr(judge, "reload_api_key", lambda: True)

    with pytest.raises(RuntimeError) as exc:
        judge._judge_anthropic("Goku vs Superman")

    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert client.call_count_state["calls"] == 2


def test_non_auth_errors_still_surface_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overload must not be swallowed by the key-rotation path."""
    client = MagicMock()
    client.messages.create.side_effect = Exception("Error code: 529 - overloaded")
    _wire(monkeypatch, client)

    called = {"reload": False}

    def reload_marker() -> bool:
        called["reload"] = True
        return True

    monkeypatch.setattr(judge, "reload_api_key", reload_marker)

    with pytest.raises(RuntimeError) as exc:
        judge._judge_anthropic("Goku vs Superman")

    assert "Anthropic request failed" in str(exc.value)
    assert called["reload"] is False, "reload must not run for non-auth failures"


def test_balance_read_does_not_swap_an_injected_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller-supplied client owns its credentials — reload must not fire."""
    client = _client_failing_first(99)
    monkeypatch.setattr(judge, "balance_model", lambda: "claude-haiku-4-5-20251001")

    called = {"reload": False}

    def reload_marker() -> bool:
        called["reload"] = True
        return True

    monkeypatch.setattr(judge, "reload_api_key", reload_marker)

    with pytest.raises(RuntimeError) as exc:
        judge.balance_read("Goku", "Superman", client=client)

    assert "ANTHROPIC_API_KEY" in str(exc.value)
    assert called["reload"] is False
    assert client.call_count_state["calls"] == 1
