"""LLM judge (Anthropic preferred, OpenAI-compatible fallback) with structured verdicts."""

from __future__ import annotations

import json
import os
from typing import Any

from bot.laws import get_laws

# Schema field order is intentional: steelman / concede / unknowns before the ruling.
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

VERDICT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matchup": {"type": "string", "description": "The matchup label, e.g. 'A vs B'"},
        "steelman_a": {
            "type": "string",
            "description": "Strongest case for fighter A, before ruling",
        },
        "steelman_b": {
            "type": "string",
            "description": "Strongest case for fighter B, before ruling",
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
        "ruling": {
            "type": "string",
            "description": "2-4 sentence ruling after steelmans and concessions",
        },
        "winner": {"type": "string", "description": "Who wins"},
        "confidence": {
            "type": "number",
            "description": "Confidence 0-10",
        },
        "citations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Canon or doctrine citations",
        },
    },
    "required": list(VERDICT_REQUIRED_FIELDS),
}

DELIVER_VERDICT_TOOL: dict[str, Any] = {
    "name": "deliver_verdict",
    "description": (
        "Deliver the court's structured verdict. Call this once with steelmans, "
        "concessions, and unknowns filled before ruling/winner/confidence."
    ),
    "input_schema": VERDICT_INPUT_SCHEMA,
}

HOUSE_RULES = """\
HOUSE RULES
1. Steelman first — present each side's strongest case before ruling.
2. Concede what's earned — acknowledge genuine advantages without hedging.
3. Canon citations beat vibes. "I don't know that material" is a legal plea, \
not a loss; put unknowns in the unknowns list.
4. Rulings carry confidence X/10 and are revisable on new evidence.
5. Traps are legal — clever setup, environment abuse, and prep are valid.
6. The migraine gets the final say. Court recesses whenever the King calls it.
"""

def _laws_block() -> str:
    laws = get_laws().strip()
    if not laws:
        return ""
    return "\nLAWS OF THE COURT (cite by name when applicable)\n" + laws + "\n"


def build_system_prompt() -> str:
    return (
        "You are Fight Club Court — a sharp analytical debate judge for fiction and "
        "death-battle matchups. You price logistics, character flaws, and win conditions, "
        "not just power levels. Tone: precise, cutting, fair.\n\n"
        f"{HOUSE_RULES}"
        f"{_laws_block()}"
        "Deliver the verdict by calling the deliver_verdict tool. Fill steelman_a, "
        "steelman_b, concessions, and unknowns before ruling, winner, confidence, and citations.\n"
    )


def build_system_prompt_openai() -> str:
    return (
        "You are Fight Club Court — a sharp analytical debate judge for fiction and "
        "death-battle matchups. You price logistics, character flaws, and win conditions, "
        "not just power levels. Tone: precise, cutting, fair.\n\n"
        f"{HOUSE_RULES}"
        f"{_laws_block()}"
        "Respond with ONLY a single JSON object matching this schema:\n"
        "{\n"
        '  "matchup": string,\n'
        '  "steelman_a": string,\n'
        '  "steelman_b": string,\n'
        '  "concessions": [string],\n'
        '  "unknowns": [string],\n'
        '  "ruling": string (2-4 sentences),\n'
        '  "winner": string,\n'
        '  "confidence": number 0-10,\n'
        '  "citations": [string]\n'
        "}\n"
    )


# Back-compat names used in tests / imports
SYSTEM_PROMPT = build_system_prompt()  # may be empty-laws until load_laws()
SYSTEM_PROMPT_OPENAI = build_system_prompt_openai()


def validate_verdict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a verdict dict."""
    if not isinstance(data, dict):
        raise ValueError("Verdict must be a dict")
    for key in VERDICT_REQUIRED_FIELDS:
        if key not in data:
            raise ValueError(f"Verdict missing required field: {key}")
    out = dict(data)
    out["confidence"] = float(out["confidence"])
    for list_key in ("citations", "concessions", "unknowns"):
        if not isinstance(out[list_key], list):
            out[list_key] = [str(out[list_key])]
    return out


def _extract_tool_verdict(resp: Any) -> dict[str, Any]:
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "tool_use" and getattr(block, "name", None) == "deliver_verdict":
            return validate_verdict(dict(block.input))
    raise ValueError("Anthropic response missing deliver_verdict tool use")


def _judge_anthropic(user_msg: str) -> dict[str, Any]:
    from anthropic import Anthropic

    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def _call() -> dict[str, Any]:
        try:
            resp = client.messages.create(
                model=model,
                system=build_system_prompt(),
                messages=[{"role": "user", "content": user_msg}],
                max_tokens=2048,
                temperature=0.4,
                tools=[DELIVER_VERDICT_TOOL],
                tool_choice={"type": "tool", "name": "deliver_verdict"},
            )
        except Exception as e:
            raise RuntimeError(f"Anthropic request failed: {e}") from e
        return _extract_tool_verdict(resp)

    try:
        return _call()
    except ValueError:
        # Retry once on validation / missing-tool failure
        return _call()


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
        return _call()
    except (ValueError, json.JSONDecodeError):
        return _call()


def judge(
    fighter_a: str,
    fighter_b: str,
    context: str | None = None,
    prior_verdict: dict[str, Any] | None = None,
    challenge: str | None = None,
) -> dict[str, Any]:
    """Return a structured verdict dict for A vs B."""
    user_parts = [
        f"Matchup: {fighter_a} vs {fighter_b}",
    ]
    if context:
        user_parts.append(f"Context / conditions: {context}")
    if prior_verdict and challenge:
        user_parts.append(
            "This is a CHALLENGE to a prior ruling. Re-judge with the new evidence. "
            "Prior verdict JSON:\n"
            + json.dumps(prior_verdict, indent=2)
            + f"\n\nChallenge / new evidence:\n{challenge}"
        )
    user_msg = "\n\n".join(user_parts)

    if os.getenv("ANTHROPIC_API_KEY"):
        return _judge_anthropic(user_msg)
    if os.getenv("OPENAI_API_KEY"):
        return _judge_openai(user_msg)
    raise RuntimeError(
        "No LLM API key set. Set ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY. "
        "Copy .env.example to .env and add your key."
    )


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
            f"Winner:   {v['winner']}",
            f"Confidence: {v['confidence']}/10",
        ]
    )
    if v.get("citations"):
        lines.append("\nCitations:")
        lines.extend(f"  - {c}" for c in v["citations"])
    return "\n".join(lines)
