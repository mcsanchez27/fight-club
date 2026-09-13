"""Deferred Discord interaction progress edits (latency UX)."""

from __future__ import annotations

from typing import Any

PROGRESS_RETRIEVING = "Retrieving receipts…"
PROGRESS_JUDGING = "Judging…"


async def edit_deferred_progress(interaction: Any, content: str) -> bool:
    """Best-effort progress edit on a deferred interaction.

    Prefers ``edit_original_response``; falls back to ``followup.send``.
    Returns True if a message was delivered. Safe to call without a live Discord loop
    when ``interaction`` is a duck-typed mock.
    """
    text = (content or "").strip()
    if not text:
        return False
    edit = getattr(interaction, "edit_original_response", None)
    if callable(edit):
        try:
            await edit(content=text)
            return True
        except Exception:
            pass
    followup = getattr(interaction, "followup", None)
    send = getattr(followup, "send", None) if followup is not None else None
    if callable(send):
        try:
            await send(text)
            return True
        except Exception:
            return False
    return False
