"""LLM judge (Anthropic preferred, OpenAI-compatible fallback) with structured verdicts."""

from __future__ import annotations

import json
import os
from typing import Any

from bot.laws import get_laws
from bot.retrieval import RetrievalResult, pack_retrieval_for_prompt, retrieve
from bot.sources import cap_snippet, is_stale

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

CITATION_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim": {"type": "string"},
        "source_url": {"type": "string"},
        "locator": {"type": "string"},
        "snippet": {"type": "string", "description": "≤25 words; no full quotes"},
        "verified": {"type": "boolean"},
        "kind": {"type": "string", "description": "receipt or exhibit"},
        "retrieved_at": {"type": "string"},
    },
    "required": ["claim"],
}

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
            "description": "Genuine advantages conceded to either side (load-bearing; cite receipts)",
        },
        "unknowns": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Material the court does not know; legal pleas, not losses",
        },
        "ruling": {
            "type": "string",
            "description": "2-4 sentence ruling after steelmans and concessions (load-bearing; cite receipts)",
        },
        "winner": {"type": "string", "description": "Who wins"},
        "confidence": {
            "type": "number",
            "description": "Confidence 0-10 (capped at 5 if retrieval unavailable)",
        },
        "citations": {
            "type": "array",
            "items": {
                "anyOf": [
                    {"type": "string"},
                    CITATION_OBJECT_SCHEMA,
                ]
            },
            "description": "Structured receipts/exhibits preferred; strings accepted and normalized",
        },
    },
    "required": list(VERDICT_REQUIRED_FIELDS),
}

DELIVER_VERDICT_TOOL: dict[str, Any] = {
    "name": "deliver_verdict",
    "description": (
        "Deliver the court's structured verdict. Call this once with steelmans, "
        "concessions, and unknowns filled before ruling/winner/confidence. "
        "Receipts are required for the ruling and every concession; optional for steelmans."
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

RECEIPTS_RULES = """\
RECEIPTS RULES
- Autonomous fetches are RECEIPTS; user-pasted text is EXHIBITS.
- Receipts are load-bearing for the ruling and every concession; optional for steelmans.
- Prefer citing packed retrieval_ids. Never invent URLs.
- Snippets ≤25 words. No full quotes.
- If retrieval is unavailable/unlisted, still rule; put gaps in unknowns; keep confidence ≤5.
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
        f"{RECEIPTS_RULES}"
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
        f"{RECEIPTS_RULES}"
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
        '  "citations": [string | {claim, source_url, locator, snippet, verified, kind}]\n'
        "}\n"
    )


# Back-compat names used in tests / imports
SYSTEM_PROMPT = build_system_prompt()  # may be empty-laws until load_laws()
SYSTEM_PROMPT_OPENAI = build_system_prompt_openai()

UNVERIFIED_CONFIDENCE_CAP = 5.0


def normalize_citation(item: Any) -> dict[str, Any]:
    """Normalize string or dict citation; cap snippet at 25 words."""
    if isinstance(item, str):
        return {
            "claim": item,
            "source_url": "",
            "locator": "",
            "snippet": cap_snippet(item),
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
        "verified": False,
        "kind": "receipt",
        "retrieved_at": "",
    }


def validate_verdict(data: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a verdict dict."""
    if not isinstance(data, dict):
        raise ValueError("Verdict must be a dict")
    for key in VERDICT_REQUIRED_FIELDS:
        if key not in data:
            raise ValueError(f"Verdict missing required field: {key}")
    out = dict(data)
    out["confidence"] = float(out["confidence"])
    for list_key in ("concessions", "unknowns"):
        if not isinstance(out[list_key], list):
            out[list_key] = [str(out[list_key])]
    raw_cites = out["citations"]
    if not isinstance(raw_cites, list):
        raw_cites = [raw_cites]
    out["citations"] = [normalize_citation(c) for c in raw_cites]
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
    *,
    exhibits: list[str] | None = None,
    franchise: str | None = None,
    skip_retrieval: bool = False,
    retrieval_result: RetrievalResult | None = None,
) -> dict[str, Any]:
    """Return a structured verdict dict for A vs B (with receipts when available)."""
    exhibit_list = list(exhibits or [])
    if challenge and challenge.strip():
        exhibit_list.append(challenge.strip())

    if retrieval_result is None and not skip_retrieval:
        retrieval_result = retrieve(
            fighter_a,
            fighter_b,
            context,
            franchise_hint=franchise,
            exhibits=exhibit_list,
        )
    elif retrieval_result is None:
        retrieval_result = RetrievalResult(
            franchise=franchise,
            status="disabled",
            receipts=[],
            exhibits=[],
        )

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

    if os.getenv("ANTHROPIC_API_KEY"):
        verdict = _judge_anthropic(user_msg)
    elif os.getenv("OPENAI_API_KEY"):
        verdict = _judge_openai(user_msg)
    else:
        raise RuntimeError(
            "No LLM API key set. Set ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY. "
            "Copy .env.example to .env and add your key."
        )

    return apply_retrieval_guardrails(verdict, retrieval_result)


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
