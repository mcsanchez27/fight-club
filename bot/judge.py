"""LLM judge (Anthropic preferred, OpenAI-compatible fallback) with structured verdicts."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from bot.laws import get_laws
from bot.prompts import (
    HOUSE_RULES_BLOCK,
    PromptTemplateError,
    render_balance_prompt,
    render_referee_prompt,
)
from bot.retrieval import RetrievalResult, pack_retrieval_for_prompt, retrieve
from bot.sources import cap_snippet, is_stale, normalize_franchise_key

log = logging.getLogger(__name__)

# Model routing (Tech Design §4 / §9). Balance helper wired for item 3.
DEFAULT_MODEL_RULING = "claude-sonnet-5"
DEFAULT_MODEL_BALANCE = "claude-haiku-4-5-20251001"
ANTHROPIC_TIMEOUT_SECONDS = 60.0
ANTHROPIC_MAX_RETRIES = 1


def ruling_model() -> str:
    """Ruling model: FIGHT_MODEL_RULING, else ANTHROPIC_MODEL (V1), else Sonnet 5 default."""
    return (
        os.getenv("FIGHT_MODEL_RULING")
        or os.getenv("ANTHROPIC_MODEL")
        or DEFAULT_MODEL_RULING
    )


def balance_model() -> str:
    """Balance-check model (Haiku). Used by item 3; constant/helper shipped in item 1."""
    return os.getenv("FIGHT_MODEL_BALANCE") or DEFAULT_MODEL_BALANCE


ANTHROPIC_AUTH_MESSAGE = (
    "Anthropic rejected the API key. Put a fresh key in .env as "
    "ANTHROPIC_API_KEY=sk-ant-... — it is re-read automatically, so a running "
    "bot does not need restarting."
)


class _AuthFailure(Exception):
    """Internal: Anthropic returned 401. Never escapes this module."""


def _is_auth_error(exc: Exception) -> bool:
    """True when the provider rejected the key (as opposed to any other error)."""
    if getattr(exc, "status_code", None) == 401:
        return True
    try:
        from anthropic import AuthenticationError

        if isinstance(exc, AuthenticationError):
            return True
    except Exception:
        pass
    text = str(exc).lower()
    return "authentication_error" in text or "api key is invalid" in text


def reload_api_key() -> bool:
    """Re-read .env after an auth failure; True when ANTHROPIC_API_KEY changed.

    Keys get rotated while the bot is running. The process loaded its
    environment once at startup, so without this a rotated key means a restart.
    """
    before = os.getenv("ANTHROPIC_API_KEY")
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except Exception:
        return False
    return os.getenv("ANTHROPIC_API_KEY") != before


def make_anthropic_client():
    """Anthropic client with V2 latency settings: timeout=60, max_retries=1."""
    from anthropic import Anthropic

    return Anthropic(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        timeout=ANTHROPIC_TIMEOUT_SECONDS,
        max_retries=ANTHROPIC_MAX_RETRIES,
    )


# V2 deliver_verdict field order (Tech Design §4). Steelman before ruling is hard.
VERDICT_FIELD_ORDER = (
    "steelman_a",
    "steelman_b",
    "exhibit_ledger",
    "concessions",
    "unknowns",
    "opening_score_a",
    "opening_score_b",
    "close_score_a",
    "close_score_b",
    "ruling",
    "winner_side",
    "confidence",
    "argument_quality",
    "citations",
)

# Tool-required fields (Amendment 11: capture scores are NOT required).
VERDICT_TOOL_REQUIRED_FIELDS = (
    "steelman_a",
    "steelman_b",
    "exhibit_ledger",
    "concessions",
    "unknowns",
    "ruling",
    "winner_side",
    "confidence",
    "citations",
)

# Soft-required core for validation (winner OR winner_side). V1 matchup optional.
# Output budget for deliver_verdict. The verdict is long-form — two steelmans,
# then the ruling, with citations LAST in VERDICT_FIELD_ORDER. At 2048 the tail
# was truncated on essentially every fight (stop_reason=max_tokens), silently
# dropping citations and sometimes `ruling` itself, which surfaced only as
# "Verdict missing required field: ruling".
RULING_MAX_OUTPUT_TOKENS_DEFAULT = 4096


def ruling_max_output_tokens() -> int:
    """Max output tokens for a ruling call; FIGHT_MAX_OUTPUT_TOKENS overrides."""
    raw = os.getenv("FIGHT_MAX_OUTPUT_TOKENS", "").strip()
    try:
        value = int(raw) if raw else RULING_MAX_OUTPUT_TOKENS_DEFAULT
    except (TypeError, ValueError):
        value = RULING_MAX_OUTPUT_TOKENS_DEFAULT
    # Never below the point where a verdict cannot physically fit.
    return max(1024, value)


VERDICT_CORE_REQUIRED = (
    "steelman_a",
    "steelman_b",
    "ruling",
    "confidence",
)

NULLABLE_SCORE_FIELDS = (
    "opening_score_a",
    "opening_score_b",
    "close_score_a",
    "close_score_b",
    "argument_quality",
)

# Back-compat alias used by older tests / imports (V1 field set; still accepted).
VERDICT_REQUIRED_FIELDS = (
    "matchup",
    "steelman_a",
    "steelman_b",
    "concessions",
    "unknowns",
    "ruling",
    "winner",
    "confidence",
    "citations",
)

CITATION_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "source_url": {"type": "string"},
        "locator": {"type": "string"},
        "snippet": {"type": "string", "description": "≤25 words; no full quotes"},
        "retrieval_id": {"type": "string"},
        "verified": {"type": "boolean"},
        "kind": {"type": "string", "description": "receipt or exhibit"},
        "retrieved_at": {"type": "string"},
    },
    "required": ["claim"],
}

EXHIBIT_LEDGER_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "exhibit_id": {"type": "string"},
        "status": {
            "type": "string",
            "description": "verified | unverified | contested",
        },
        "weight_note": {"type": "string"},
    },
    "required": ["exhibit_id", "status"],
}

VERDICT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steelman_a": {
            "type": "string",
            "description": "Strongest case for side A, before ruling",
        },
        "steelman_b": {
            "type": "string",
            "description": "Strongest case for side B, before ruling",
        },
        "exhibit_ledger": {
            "type": "array",
            "items": EXHIBIT_LEDGER_ITEM_SCHEMA,
            "description": "Per-exhibit status and weight notes",
        },
        "concessions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Genuine advantages conceded to either side",
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Material the court does not know; legal pleas, not losses",
        },
        "opening_score_a": {
            "type": ["number", "null"],
            "description": "Opening argument quality for A, 1–10 (nullable)",
        },
        "opening_score_b": {
            "type": ["number", "null"],
            "description": "Opening argument quality for B, 1–10 (nullable)",
        },
        "close_score_a": {
            "type": ["number", "null"],
            "description": "Closing argument quality for A, 1–10 (nullable)",
        },
        "close_score_b": {
            "type": ["number", "null"],
            "description": "Closing argument quality for B, 1–10 (nullable)",
        },
        "ruling": {
            "type": "string",
            "description": "2-4 sentence ruling after steelmans and concessions",
        },
        "winner_side": {
            "type": "string",
            "enum": ["a", "b"],
            "description": "Winning side — a tie is not an output",
        },
        "confidence": {
            "type": "number",
            "description": "Confidence 0-10, one decimal (capped at 5 if retrieval unavailable)",
        },
        "argument_quality": {
            "type": ["number", "null"],
            "description": "Overall advocacy quality 1–10 (nullable)",
        },
        "citations": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    CITATION_OBJECT_SCHEMA,
                ]
            },
            "description": "Structured receipts/exhibits; strings accepted and normalized",
        },
        # V1 / instant soft-compat (optional; not in V2 required order)
        "matchup": {"type": "string", "description": "Optional matchup label (V1/instant)"},
        "winner": {"type": "string", "description": "Optional free-text winner (V1); prefer winner_side"},
    },
    "required": list(VERDICT_TOOL_REQUIRED_FIELDS),
}

DELIVER_VERDICT_TOOL: dict[str, Any] = {
    "type": "custom",
    "name": "deliver_verdict",
    "description": (
        "Deliver the court's structured verdict. Call this once. "
        "Steelman_a, steelman_b, exhibit_ledger, concessions, and unknowns "
        "MUST be filled before ruling / winner_side / confidence. "
        "Opening/close/argument_quality may be null if the record is thin. "
        "Receipts are required for the ruling and every concession."
    ),
    "input_schema": VERDICT_INPUT_SCHEMA,
}

BALANCE_READ_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {
            "type": "number",
            "minimum": 1,
            "maximum": 10,
            "description": "1–10; 10 = even",
        },
        "favored_side": {
            "type": "string",
            "enum": ["a", "b", "even"],
        },
        "reason": {
            "type": "string",
            "description": "≤25 words",
        },
        "franchise_a": {
            "type": "string",
            "description": "Free-text franchise for side A (normalized after return)",
        },
        "franchise_b": {
            "type": "string",
            "description": "Free-text franchise for side B (normalized after return)",
        },
    },
    "required": ["score", "favored_side", "reason", "franchise_a", "franchise_b"],
}

BALANCE_READ_TOOL: dict[str, Any] = {
    "type": "custom",
    "name": "balance_read",
    "description": (
        "Return a pre-fight balance read for the challenge card. "
        "Call once. score 10 = even; favored_side a|b|even; reason ≤25 words; "
        "franchise_a/franchise_b are free-text labels for retrieval routing."
    ),
    "input_schema": BALANCE_READ_INPUT_SCHEMA,
}

# Re-export for callers that previously imported HOUSE_RULES from judge.
HOUSE_RULES = HOUSE_RULES_BLOCK


def build_system_prompt(
    *,
    fight_setup: str = "",
    receipts: str = "",
    transcript: str = "",
    exhibit_ledger: str = "",
    prior_ruling: str = "",
) -> str:
    """Referee system prompt from prompts/referee.md (House Rules + Laws injected)."""
    return render_referee_prompt(
        laws=get_laws(),
        house_rules=HOUSE_RULES_BLOCK,
        fight_setup=fight_setup,
        receipts=receipts,
        transcript=transcript,
        exhibit_ledger=exhibit_ledger,
        prior_ruling=prior_ruling,
    )


def build_system_prompt_openai(
    *,
    fight_setup: str = "",
    receipts: str = "",
    transcript: str = "",
    exhibit_ledger: str = "",
    prior_ruling: str = "",
) -> str:
    """OpenAI fallback: same disk template + JSON object instruction (V1 soft path)."""
    base = build_system_prompt(
        fight_setup=fight_setup,
        receipts=receipts,
        transcript=transcript,
        exhibit_ledger=exhibit_ledger,
        prior_ruling=prior_ruling,
    )
    return (
        base
        + "\nRespond with ONLY a single JSON object matching the deliver_verdict "
        "fields (steelman before ruling). Include winner_side ('a'|'b') or winner; "
        "opening/close/argument_quality may be null.\n"
    )


# Back-compat names used in tests / imports (loaded from disk at import time).
try:
    SYSTEM_PROMPT = build_system_prompt()
    SYSTEM_PROMPT_OPENAI = build_system_prompt_openai()
except PromptTemplateError:
    SYSTEM_PROMPT = ""
    SYSTEM_PROMPT_OPENAI = ""

UNVERIFIED_CONFIDENCE_CAP = 5.0


def normalize_citation(item: Any) -> dict[str, Any]:
    """Normalize string or dict citation; cap snippet at 25 words."""
    if isinstance(item, str):
        return {
            "claim": item,
            "source_url": "",
            "locator": "",
            "snippet": cap_snippet(item),
            "retrieval_id": "",
            "verified": False,
            "kind": "receipt",
            "retrieved_at": "",
        }
    if isinstance(item, dict):
        out = {
            "claim": str(item.get("claim") or item.get("text") or ""),
            "source_url": str(item.get("source_url") or item.get("url") or ""),
            "locator": str(item.get("locator") or ""),
            "snippet": cap_snippet(str(item.get("snippet") or item.get("quote") or "")),
            "retrieval_id": str(item.get("retrieval_id") or ""),
            "verified": bool(item.get("verified", False)),
            "kind": str(item.get("kind") or "receipt"),
            "retrieved_at": str(item.get("retrieved_at") or ""),
        }
        if not out["claim"]:
            out["claim"] = out["snippet"] or out["locator"] or "citation"
        if out.get("verified") and is_stale(out.get("retrieved_at") or None):
            out["verified"] = False
            out["stale"] = True
        return out
    return {
        "claim": str(item),
        "source_url": "",
        "locator": "",
        "snippet": "",
        "retrieval_id": "",
        "verified": False,
        "kind": "receipt",
        "retrieved_at": "",
    }


def _coerce_optional_score(value: Any) -> float | None:
    """Nullable 1–10 capture score (Amendment 11 — missing must not fail)."""
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n


def _map_winner_fields(out: dict[str, Any]) -> None:
    """Keep winner (V1/CLI) and winner_side (V2) in sync when possible."""
    side = out.get("winner_side")
    winner = out.get("winner")
    if side is not None and str(side).strip():
        side_l = str(side).strip().lower()
        if side_l in {"a", "b"}:
            out["winner_side"] = side_l
            if not winner:
                out["winner"] = side_l.upper()
            return
    if winner is not None and str(winner).strip():
        w = str(winner).strip()
        out["winner"] = w
        wl = w.lower()
        if wl in {"a", "b"} and not side:
            out["winner_side"] = wl
        elif not side:
            # Free-text V1 winner — leave winner_side unset for soft path.
            out.setdefault("winner_side", None)
        return
    raise ValueError("Verdict missing required field: winner_side (or winner)")


def validate_verdict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a verdict dict (V2 soft migration + V1 CLI).

    Core required: steelman_a/b, ruling, confidence, and winner_side or winner.
    Amendment 11: opening/close/argument_quality may be missing → null.
    V1 fields (matchup, winner, string citations) still accepted.
    """
    if not isinstance(data, dict):
        raise ValueError("Verdict must be a dict")
    for key in VERDICT_CORE_REQUIRED:
        if key not in data:
            raise ValueError(f"Verdict missing required field: {key}")
    out = dict(data)
    _map_winner_fields(out)

    out["confidence"] = float(out["confidence"])
    # Round display-style one-decimal guidance without rejecting ints.
    out["confidence"] = round(out["confidence"], 1)

    for list_key in ("concessions", "unknowns"):
        if list_key not in out or out[list_key] is None:
            out[list_key] = []
        elif not isinstance(out[list_key], list):
            out[list_key] = [str(out[list_key])]

    if "exhibit_ledger" not in out or out["exhibit_ledger"] is None:
        out["exhibit_ledger"] = []
    elif not isinstance(out["exhibit_ledger"], list):
        out["exhibit_ledger"] = [out["exhibit_ledger"]]
    ledger: list[dict[str, Any]] = []
    for item in out["exhibit_ledger"]:
        if isinstance(item, dict):
            ledger.append(
                {
                    "exhibit_id": str(item.get("exhibit_id") or ""),
                    "status": str(item.get("status") or "unverified"),
                    "weight_note": str(item.get("weight_note") or ""),
                }
            )
        else:
            ledger.append(
                {"exhibit_id": str(item), "status": "unverified", "weight_note": ""}
            )
    out["exhibit_ledger"] = ledger

    for score_key in NULLABLE_SCORE_FIELDS:
        if score_key not in out:
            out[score_key] = None
        else:
            out[score_key] = _coerce_optional_score(out.get(score_key))

    if "citations" not in out or out["citations"] is None:
        raw_cites: list[Any] = []
    else:
        raw_cites = out["citations"]
        if not isinstance(raw_cites, list):
            raw_cites = [raw_cites]
    out["citations"] = [normalize_citation(c) for c in raw_cites]

    if "matchup" not in out or out["matchup"] is None:
        out["matchup"] = ""
    return out


def apply_retrieval_guardrails(
    verdict: dict[str, Any], result: RetrievalResult
) -> dict[str, Any]:
    """Cap confidence, merge packed receipts/exhibits, set retrieval metadata."""
    out = dict(verdict)
    out["retrieval_status"] = result.status
    out["franchise"] = result.franchise
    out["voided"] = bool(result.retrieval_unavailable)

    if result.retrieval_unavailable:
        out["confidence"] = min(float(out["confidence"]), UNVERIFIED_CONFIDENCE_CAP)
        unknowns = list(out.get("unknowns") or [])
        plea = "unverified: retrieval unavailable"
        if plea not in unknowns:
            unknowns.append(plea)
        out["unknowns"] = unknowns

    # Prefer packed structured passages; append any model citations not already present
    packed = [p.to_dict() for p in (result.receipts + result.exhibits)]
    model_cites = [normalize_citation(c) for c in (out.get("citations") or [])]
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in packed + model_cites:
        key = f"{c.get('kind')}|{c.get('source_url')}|{c.get('claim')}|{c.get('snippet')}"
        if key in seen:
            continue
        seen.add(key)
        c["snippet"] = cap_snippet(str(c.get("snippet") or ""))
        merged.append(c)

    # Code owns verified: never trust model-minted verified=true.
    packed_verified_urls = {
        p.source_url for p in result.receipts if p.verified and p.source_url
    }
    packed_exhibit_verified = {
        p.source_url for p in result.exhibits if p.verified and p.source_url
    }
    for c in merged:
        url = c.get("source_url") or ""
        kind = c.get("kind") or "receipt"
        textblob = f"{c.get('claim', '')} {c.get('locator', '')} {url}".lower()
        if "laws of the court" in textblob or "house rule" in textblob:
            c["kind"] = "exhibit"
            c["verified"] = False
        elif kind == "receipt":
            c["verified"] = bool(url and url in packed_verified_urls)
        elif kind == "exhibit":
            c["verified"] = bool(url and url in packed_exhibit_verified)
        else:
            c["verified"] = False
        if c.get("verified") and is_stale(c.get("retrieved_at") or None):
            c["verified"] = False
            c["stale"] = True

    out["citations"] = merged
    return out


def _truncated(resp: Any) -> bool:
    return getattr(resp, "stop_reason", None) == "max_tokens"


def _extract_tool_verdict(resp: Any) -> dict[str, Any]:
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", None) == "deliver_verdict":
            payload = dict(block.input)
            if _truncated(resp):
                missing = [f for f in VERDICT_CORE_REQUIRED if f not in payload]
                if missing:
                    # Don't report this as a malformed verdict — the model was
                    # cut off mid-write, which is a budget problem, not its fault.
                    raise ValueError(
                        "Ruling was cut off at the output limit "
                        f"({ruling_max_output_tokens()} tokens) before writing: "
                        f"{', '.join(missing)}. Raise FIGHT_MAX_OUTPUT_TOKENS."
                    )
                # Complete enough to rule, but the tail is gone. Citations are
                # last in VERDICT_FIELD_ORDER, so they are what gets lost.
                log.warning(
                    "Verdict truncated at %d output tokens; trailing fields "
                    "(citations, argument_quality) may be missing.",
                    ruling_max_output_tokens(),
                )
            return validate_verdict(payload)
    raise ValueError("Anthropic response missing deliver_verdict tool use")


def _usage_from_response(resp: Any) -> dict[str, int]:
    """Pull real input/output token counts from an Anthropic Messages response."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0}
    tin = getattr(usage, "input_tokens", None)
    tout = getattr(usage, "output_tokens", None)
    if tin is None and isinstance(usage, dict):
        tin = usage.get("input_tokens", 0)
        tout = usage.get("output_tokens", 0)
    return {
        "input_tokens": int(tin or 0),
        "output_tokens": int(tout or 0),
    }


def _attach_usage(verdict: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    out = dict(verdict)
    out["_usage"] = dict(usage)
    return out


def _judge_anthropic(user_msg: str) -> dict[str, Any]:
    model = ruling_model()
    client = make_anthropic_client()

    def _call() -> tuple[dict[str, Any], dict[str, int]]:
        try:
            resp = client.messages.create(
                model=model,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=ruling_max_output_tokens(),
                tools=[DELIVER_VERDICT_TOOL],
                tool_choice={"type": "tool", "name": "deliver_verdict"},
            )
        except Exception as e:
            if _is_auth_error(e):
                raise _AuthFailure(str(e)) from e
            raise RuntimeError(f"Anthropic request failed: {e}") from e
        return _extract_tool_verdict(resp), _usage_from_response(resp)

    try:
        verdict, usage = _call()
    except ValueError:
        # Retry once on validation / missing-tool failure (app-level; SDK max_retries=1)
        verdict, usage = _call()
    except _AuthFailure:
        # The key may have been rotated since startup. Re-read .env and retry
        # once rather than making an operator restart the bot mid-fight.
        if not reload_api_key():
            raise RuntimeError(ANTHROPIC_AUTH_MESSAGE) from None
        client = make_anthropic_client()
        try:
            verdict, usage = _call()
        except _AuthFailure:
            raise RuntimeError(ANTHROPIC_AUTH_MESSAGE) from None
    return _attach_usage(verdict, usage)


def _judge_openai(user_msg: str) -> dict[str, Any]:
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    client = OpenAI(**kwargs)
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def _call() -> dict[str, Any]:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": build_system_prompt_openai()},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.4,
                response_format={"type": "json_object"},
            )
        except Exception as e:
            raise RuntimeError(f"OpenAI request failed: {e}") from e
        content = resp.choices[0].message.content or ""
        return validate_verdict(json.loads(content))

    try:
        verdict = _call()
    except (ValueError, json.JSONDecodeError):
        verdict = _call()
    # OpenAI path: no guaranteed usage object in all providers — leave zeros for caller fallback.
    return _attach_usage(verdict, {"input_tokens": 0, "output_tokens": 0})


def validate_balance(data: dict[str, Any]) -> dict[str, Any]:
    """Validate balance_read tool payload and normalize franchise keys (Q5)."""
    if not isinstance(data, dict):
        raise ValueError("Balance read must be a dict")
    for key in ("score", "favored_side", "reason", "franchise_a", "franchise_b"):
        if key not in data:
            raise ValueError(f"Balance read missing required field: {key}")
    out = dict(data)
    score = float(out["score"])
    if score < 1 or score > 10:
        raise ValueError("Balance score must be between 1 and 10")
    out["score"] = score
    side = str(out["favored_side"]).strip().lower()
    if side not in {"a", "b", "even"}:
        raise ValueError("favored_side must be 'a', 'b', or 'even'")
    out["favored_side"] = side
    out["reason"] = cap_snippet(str(out.get("reason") or ""), 25)

    raw_a = "" if out.get("franchise_a") is None else str(out.get("franchise_a"))
    raw_b = "" if out.get("franchise_b") is None else str(out.get("franchise_b"))
    out["franchise_a_raw"] = raw_a.strip()
    out["franchise_b_raw"] = raw_b.strip()
    key_a = normalize_franchise_key(raw_a)
    key_b = normalize_franchise_key(raw_b)
    out["franchise_a"] = key_a  # None → unlisted / plea for that side
    out["franchise_b"] = key_b
    out["franchise_a_unlisted"] = key_a is None and bool(raw_a.strip())
    out["franchise_b_unlisted"] = key_b is None and bool(raw_b.strip())
    # Empty raw also counts as unlisted for retrieval routing.
    if not raw_a.strip():
        out["franchise_a_unlisted"] = True
    if not raw_b.strip():
        out["franchise_b_unlisted"] = True
    return out


def _extract_balance_tool(resp: Any) -> dict[str, Any]:
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", None) == "balance_read":
            return validate_balance(dict(block.input))
    raise ValueError("Anthropic response missing balance_read tool use")


def balance_read(
    side_a: str,
    side_b: str,
    context: str | None = None,
    *,
    client: Any | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Pre-fight balance check via Haiku + balance_read tool.

    Callable helper for item 4c challenge cards. Mock ``client`` in tests
    (any object with ``messages.create``). No Discord wiring.
    """
    matchup = f"{side_a} vs {side_b}"
    user_msg = render_balance_prompt(matchup=matchup, context=context)
    # System is intentionally short; the template already carries instructions.
    system = (
        "You are the Fight Club balance reader. Call balance_read once. "
        "Do not rule the fight."
    )
    use_model = model or balance_model()
    injected_client = client is not None
    anthropic_client = client if injected_client else make_anthropic_client()

    def _call() -> tuple[dict[str, Any], dict[str, int]]:
        try:
            resp = anthropic_client.messages.create(
                model=use_model,
                system=system,
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=512,
                tools=[BALANCE_READ_TOOL],
                tool_choice={"type": "tool", "name": "balance_read"},
            )
        except Exception as e:
            if _is_auth_error(e):
                raise _AuthFailure(str(e)) from e
            raise RuntimeError(f"Balance read request failed: {e}") from e
        return _extract_balance_tool(resp), _usage_from_response(resp)

    try:
        result, usage = _call()
    except ValueError:
        result, usage = _call()
    except _AuthFailure:
        # A caller-supplied client owns its own credentials — do not swap it.
        if injected_client or not reload_api_key():
            raise RuntimeError(ANTHROPIC_AUTH_MESSAGE) from None
        anthropic_client = make_anthropic_client()
        try:
            result, usage = _call()
        except _AuthFailure:
            raise RuntimeError(ANTHROPIC_AUTH_MESSAGE) from None
    result["_usage"] = {
        **usage,
        "role": "balance",
        "model": use_model,
    }
    return result


# Alias for callers / docs that prefer judge_balance naming.
judge_balance = balance_read


def judge_with_materials(
    *,
    fight_setup: str = "",
    receipts: str = "",
    transcript: str = "",
    exhibit_ledger: str = "",
    prior_ruling: str = "",
    user_msg: str | None = None,
    client: Any | None = None,
    model: str | None = None,
    retrieval_result: RetrievalResult | None = None,
) -> dict[str, Any]:
    """V2 ruling call: referee template slots + deliver_verdict tool.

    Mock ``client`` (object with ``messages.create``) in tests. When
    ``retrieval_result`` is provided, applies Phase-3 guardrails (confidence
    cap / citation merge). Never refuses for missing receipts (House Rule 3).
    """
    system = build_system_prompt(
        fight_setup=fight_setup,
        receipts=receipts,
        transcript=transcript,
        exhibit_ledger=exhibit_ledger,
        prior_ruling=prior_ruling,
    )
    msg = user_msg or (
        "Deliver the verdict for this fight by calling deliver_verdict once. "
        "Steelman both sides before the ruling. Fill exhibit_ledger notes from "
        "the provided ledger. Opening/close/argument_quality may be null if the "
        "record is thin."
    )
    use_model = model or ruling_model()
    anthropic_client = client if client is not None else None

    def _call_anthropic(c: Any) -> tuple[dict[str, Any], dict[str, int]]:
        try:
            resp = c.messages.create(
                model=use_model,
                system=system,
                messages=[{"role": "user", "content": msg}],
                max_tokens=ruling_max_output_tokens(),
                tools=[DELIVER_VERDICT_TOOL],
                tool_choice={"type": "tool", "name": "deliver_verdict"},
            )
        except Exception as e:
            raise RuntimeError(f"Anthropic request failed: {e}") from e
        return _extract_tool_verdict(resp), _usage_from_response(resp)

    t_judge = time.monotonic()
    if anthropic_client is not None or os.getenv("ANTHROPIC_API_KEY"):
        c = anthropic_client if anthropic_client is not None else make_anthropic_client()
        try:
            verdict, usage = _call_anthropic(c)
        except ValueError:
            verdict, usage = _call_anthropic(c)
    elif os.getenv("OPENAI_API_KEY"):
        # Soft OpenAI path with the same filled template.
        from openai import OpenAI

        api_key = os.getenv("OPENAI_API_KEY")
        kwargs: dict[str, Any] = {"api_key": api_key}
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        oai = OpenAI(**kwargs)
        oai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        oai_system = (
            system
            + "\nRespond with ONLY a single JSON object matching the deliver_verdict "
            "fields (steelman before ruling). Include winner_side ('a'|'b') or winner; "
            "opening/close/argument_quality may be null.\n"
        )

        def _oai() -> dict[str, Any]:
            resp = oai.chat.completions.create(
                model=oai_model,
                messages=[
                    {"role": "system", "content": oai_system},
                    {"role": "user", "content": msg},
                ],
                temperature=0.4,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            return validate_verdict(json.loads(content))

        try:
            verdict = _oai()
        except (ValueError, json.JSONDecodeError):
            verdict = _oai()
        usage = {"input_tokens": 0, "output_tokens": 0}
    else:
        raise RuntimeError(
            "No LLM API key set. Set ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY. "
            "Copy .env.example to .env and add your key."
        )
    judge_seconds = time.monotonic() - t_judge
    usage_out = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "retrieval_seconds": 0.0,
        "judge_seconds": round(judge_seconds, 4),
        "total_seconds": round(judge_seconds, 4),
        "role": "ruling",
        "model": use_model,
    }
    verdict = _attach_usage(verdict, usage_out)
    if retrieval_result is not None:
        guarded = apply_retrieval_guardrails(verdict, retrieval_result)
        guarded["_usage"] = usage_out
        return guarded
    return verdict


def judge(
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
    prior_verdict: dict[str, Any] | None = None,
    challenge: str | None = None,
    *,
    exhibits: list[str] | None = None,
    franchise: str | None = None,
    skip_retrieval: bool = False,
    retrieval_result: RetrievalResult | None = None,
) -> dict[str, Any]:
    """Return a structured verdict dict for A vs B (with receipts when available).

    Attaches ``_usage`` with real Anthropic token counts (when available) and stage
    timings: retrieval_seconds, judge_seconds, total_seconds.
    """
    t_total = time.monotonic()
    exhibit_list = list(exhibits or [])
    if challenge and challenge.strip():
        exhibit_list.append(challenge.strip())

    retrieval_seconds = 0.0
    if retrieval_result is None and not skip_retrieval:
        t_ret = time.monotonic()
        retrieval_result = retrieve(
            fighter_a,
            fighter_b,
            context,
            franchise_hint=franchise,
            exhibits=exhibit_list,
        )
        retrieval_seconds = float(
            getattr(retrieval_result, "retrieval_seconds", 0.0) or (time.monotonic() - t_ret)
        )
    elif retrieval_result is None:
        retrieval_result = RetrievalResult(
            franchise=franchise,
            status="disabled",
            receipts=[],
            exhibits=[],
            retrieval_seconds=0.0,
        )
    else:
        retrieval_seconds = float(getattr(retrieval_result, "retrieval_seconds", 0.0) or 0.0)

    user_parts = [
        f"Matchup: {fighter_a} vs {fighter_b}",
    ]
    if context:
        user_parts.append(f"Context / conditions: {context}")
    user_parts.append(pack_retrieval_for_prompt(retrieval_result))
    if prior_verdict and challenge:
        user_parts.append(
            "This is a CHALLENGE to a prior ruling. Re-judge with the new evidence. "
            "Prior verdict JSON:\n"
            + json.dumps(prior_verdict, indent=2)
            + f"\n\nChallenge / new evidence:\n{challenge}"
        )
    user_msg = "\n\n".join(user_parts)

    t_judge = time.monotonic()
    if os.getenv("ANTHROPIC_API_KEY"):
        verdict = _judge_anthropic(user_msg)
    elif os.getenv("OPENAI_API_KEY"):
        verdict = _judge_openai(user_msg)
    else:
        raise RuntimeError(
            "No LLM API key set. Set ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY. "
            "Copy .env.example to .env and add your key."
        )
    judge_seconds = time.monotonic() - t_judge
    total_seconds = retrieval_seconds + judge_seconds

    # Preserve any provider token counts; always stamp stage timings.
    usage = dict(verdict.get("_usage") or {})
    usage.update(
        {
            "retrieval_seconds": round(retrieval_seconds, 4),
            "judge_seconds": round(judge_seconds, 4),
            "total_seconds": round(total_seconds, 4),
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }
    )
    # Wall clock for the whole judge() call (includes prompt pack); keep additive stages above.
    usage["wall_seconds"] = round(time.monotonic() - t_total, 4)
    verdict = _attach_usage(verdict, usage)

    guarded = apply_retrieval_guardrails(verdict, retrieval_result)
    # apply_retrieval_guardrails copies the dict but may drop unknown keys — reattach.
    guarded["_usage"] = usage
    return guarded


def format_verdict_text(v: dict[str, Any]) -> str:
    """Plain-text CLI rendering of a verdict (steelman-first)."""
    lines = [
        f"Matchup:  {v['matchup']}",
        "",
        f"Steelman A: {v['steelman_a']}",
        f"Steelman B: {v['steelman_b']}",
    ]
    if v.get("concessions"):
        lines.append("\nConcessions:")
        lines.extend(f"  - {c}" for c in v["concessions"])
    if v.get("unknowns"):
        lines.append("\nUnknowns:")
        lines.extend(f"  - {u}" for u in v["unknowns"])
    lines.extend(
        [
            "",
            f"Ruling: {v['ruling']}",
            f"Winner:   {v.get('winner') or v.get('winner_side') or '?'}",
            f"Confidence: {v['confidence']}/10",
        ]
    )
    if v.get("retrieval_status"):
        lines.append(f"Retrieval: {v['retrieval_status']}")
    if v.get("citations"):
        lines.append("\nCitations:")
        for c in v["citations"]:
            if isinstance(c, dict):
                flag = "verified" if c.get("verified") else "unverified"
                kind = c.get("kind") or "receipt"
                bit = f"[{kind}/{flag}] {c.get('claim', '')}"
                if c.get("locator"):
                    bit += f" · {c['locator']}"
                if c.get("source_url"):
                    bit += f" · {c['source_url']}"
                lines.append(f"  - {bit}")
            else:
                lines.append(f"  - {c}")
    return "\n".join(lines)
