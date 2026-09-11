"""Load Laws of the Court from laws.md."""

from __future__ import annotations

from pathlib import Path

LAWS_PATH = Path(__file__).resolve().parent.parent / "laws.md"

_laws_text: str = ""


def load_laws(path: Path | None = None) -> str:
    """Load laws markdown from disk (call at startup)."""
    global _laws_text
    p = path if path is not None else LAWS_PATH
    _laws_text = p.read_text(encoding="utf-8")
    return _laws_text


def get_laws() -> str:
    """Return cached laws text; load from default path if not yet loaded."""
    global _laws_text
    if not _laws_text:
        if LAWS_PATH.exists():
            load_laws()
        else:
            _laws_text = ""
    return _laws_text
