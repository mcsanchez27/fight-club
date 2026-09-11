"""OpenAI-compatible judge with house rules and structured JSON verdicts."""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

SYSTEM_PROMPT = """\
You are Fight Club Court — a sharp analytical debate judge for fiction and \
death-battle matchups. You price logistics, character flaws, and win conditions, \
not just power levels. Tone: precise, cutting, fair.

HOUSE RULES
1. Steelman first — present each side's strongest case before ruling.
2. Concede what's earned — acknowledge genuine advantages without hedging.
3. Canon citations beat vibes. "I don't know that material" is a legal plea, \
not a loss; put unknowns in the unknowns list.
4. Rulings carry confidence X/10 and are revisable on new evidence.
5. Traps are legal — clever setup, environment abuse, and prep are valid.
6. Transparent confidence beats fake neutrality — state your lean clearly.

Respond with ONLY a single JSON object matching this schema (no markdown fences):
{
  "matchup": string,
  "winner": string,
  "confidence": number 0-10,
  "ruling": string (2-4 sentences),
  "steelman_a": string,
  "steelman_b": string,
  "citations": [string],
  "concessions": [string],
  "unknowns": [string]
}
"""


def _client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    kwargs: dict[str, Any] = {"api_key": api_key}
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _parse_verdict(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # drop opening fence and optional closing fence
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    data = json.loads(text)
    required = (
        "matchup",
        "winner",
        "confidence",
        "ruling",
        "steelman_a",
        "steelman_b",
        "citations",
        "concessions",
        "unknowns",
    )
    for key in required:
        if key not in data:
            raise ValueError(f"Verdict missing required field: {key}")
    data["confidence"] = float(data["confidence"])
    for list_key in ("citations", "concessions", "unknowns"):
        if not isinstance(data[list_key], list):
            data[list_key] = [str(data[list_key])]
    return data


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

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = _client()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        raise RuntimeError(f"OpenAI request failed: {e}") from e

    content = resp.choices[0].message.content or ""
    return _parse_verdict(content)


def format_verdict_text(v: dict[str, Any]) -> str:
    """Plain-text CLI rendering of a verdict."""
    lines = [
        f"Matchup:  {v['matchup']}",
        f"Winner:   {v['winner']}",
        f"Confidence: {v['confidence']}/10",
        "",
        f"Ruling: {v['ruling']}",
        "",
        f"Steelman A: {v['steelman_a']}",
        f"Steelman B: {v['steelman_b']}",
    ]
    if v.get("citations"):
        lines.append("\nCitations:")
        lines.extend(f"  - {c}" for c in v["citations"])
    if v.get("concessions"):
        lines.append("\nConcessions:")
        lines.extend(f"  - {c}" for c in v["concessions"])
    if v.get("unknowns"):
        lines.append("\nUnknowns:")
        lines.extend(f"  - {u}" for u in v["unknowns"])
    return "\n".join(lines)
