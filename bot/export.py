"""Markdown export of a ruling for paste-anywhere."""

from __future__ import annotations

from typing import Any


def _flag(verified: bool) -> str:
    return "verified" if verified else "unverified"


def citation_lines(citations: list[Any]) -> list[str]:
    lines: list[str] = []
    for c in citations or []:
        if isinstance(c, str):
            lines.append(f"- {c}")
            continue
        if not isinstance(c, dict):
            lines.append(f"- {c}")
            continue
        kind = c.get("kind") or "receipt"
        verified = bool(c.get("verified"))
        claim = c.get("claim") or ""
        loc = c.get("locator") or ""
        url = c.get("source_url") or ""
        snip = c.get("snippet") or ""
        head = f"- [{kind}/{_flag(verified)}] {claim}".strip()
        detail_bits = [b for b in (loc, url, f'"{snip}"' if snip else "") if b]
        if detail_bits:
            head += " — " + " · ".join(detail_bits)
        lines.append(head)
    return lines


def export_markdown(
    verdict: dict[str, Any],
    *,
    fighter_a: str | None = None,
    fighter_b: str | None = None,
    context: str | None = None,
    franchise: str | None = None,
    retrieval_status: str | None = None,
    voided: bool = False,
    citations: list[Any] | None = None,
) -> str:
    """Return a fenced markdown block suitable for Discord / Notion / notes apps."""
    matchup = verdict.get("matchup") or (
        f"{fighter_a} vs {fighter_b}" if fighter_a and fighter_b else "Matchup"
    )
    conf = verdict.get("confidence", "?")
    status = retrieval_status or verdict.get("retrieval_status")
    cites = citations if citations is not None else verdict.get("citations") or []

    lines: list[str] = [
        f"# {matchup}",
        "",
    ]
    if fighter_a and fighter_b:
        lines.append(f"**Fighters:** {fighter_a} vs {fighter_b}")
    if context:
        lines.append(f"**Context:** {context}")
    if franchise:
        lines.append(f"**Franchise:** {franchise}")
    if status:
        lines.append(f"**Retrieval:** {status}")
    if voided or verdict.get("voided"):
        lines.append("**Status:** voided — awaiting re-judge when retrieval returns")
    if status in {"unavailable", "unlisted", "disabled"}:
        lines.append("**Banner:** unverified: retrieval unavailable")
    lines.extend(
        [
            "",
            "## Steelman A",
            str(verdict.get("steelman_a") or "—"),
            "",
            "## Steelman B",
            str(verdict.get("steelman_b") or "—"),
            "",
            "## Concessions",
        ]
    )
    concessions = verdict.get("concessions") or []
    if concessions:
        lines.extend(f"- {c}" for c in concessions)
    else:
        lines.append("- —")
    lines.extend(["", "## Unknowns"])
    unknowns = verdict.get("unknowns") or []
    if unknowns:
        lines.extend(f"- {u}" for u in unknowns)
    else:
        lines.append("- —")
    lines.extend(
        [
            "",
            "## Ruling",
            str(verdict.get("ruling") or "—"),
            "",
            f"**Winner:** {verdict.get('winner', '?')}",
            f"**Confidence:** {conf}/10",
            "",
            "## Citations",
        ]
    )
    cite_lines = citation_lines(list(cites))
    if cite_lines:
        lines.extend(cite_lines)
    else:
        lines.append("- —")
    lines.append("")
    body = "\n".join(lines)
    return f"```markdown\n{body}\n```"
