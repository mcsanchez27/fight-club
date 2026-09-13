"""Load Fight Club prompt templates from disk (never hardcode long prompts)."""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Injection slots documented for callers / templates.
HOUSE_RULES_SLOT = "{{HOUSE_RULES}}"
LAWS_SLOT = "{{LAWS}}"
FIGHT_SETUP_SLOT = "{{FIGHT_SETUP}}"
RECEIPTS_SLOT = "{{RECEIPTS}}"
TRANSCRIPT_SLOT = "{{TRANSCRIPT}}"
EXHIBIT_LEDGER_SLOT = "{{EXHIBIT_LEDGER}}"
PRIOR_RULING_SLOT = "{{PRIOR_RULING}}"
MATCHUP_SLOT = "{{MATCHUP}}"
CONTEXT_SLOT = "{{CONTEXT}}"

HOUSE_RULES_BLOCK = """\
HOUSE RULES
1. Steelman first — present each side's strongest case before ruling.
2. Concede what's earned — acknowledge genuine advantages without hedging.
3. Canon citations beat vibes. "I don't know that material" is a legal plea, \
not a loss; put unknowns in the unknowns list.
4. Rulings carry confidence X/10 and are revisable on new evidence.
5. Traps are legal — clever setup, environment abuse, and prep are valid.
6. The migraine gets the final say. Court recesses whenever the King calls it.
"""


class PromptTemplateError(FileNotFoundError):
    """Raised when a required prompt template file is missing on disk."""


def prompt_path(name: str) -> Path:
    """Resolve a template filename under prompts/."""
    safe = Path(name).name
    return PROMPTS_DIR / safe


def load_prompt(name: str, *, prompts_dir: Path | None = None) -> str:
    """Load a markdown prompt template from disk.

    Raises PromptTemplateError with a clear path if the file is missing.
    """
    base = prompts_dir if prompts_dir is not None else PROMPTS_DIR
    path = (base / Path(name).name) if prompts_dir is not None else prompt_path(name)
    if not path.is_file():
        raise PromptTemplateError(
            f"Prompt template missing: {path} "
            f"(expected prompts/{Path(name).name} on disk)"
        )
    return path.read_text(encoding="utf-8")


def _fill(template: str, mapping: dict[str, str]) -> str:
    out = template
    for key, value in mapping.items():
        out = out.replace(key, value if value is not None else "")
    return out


def render_referee_prompt(
    *,
    laws: str = "",
    house_rules: str | None = None,
    fight_setup: str = "",
    receipts: str = "",
    transcript: str = "",
    exhibit_ledger: str = "",
    prior_ruling: str = "",
    prompts_dir: Path | None = None,
) -> str:
    """Render prompts/referee.md with House Rules / Laws and fight slots."""
    template = load_prompt("referee.md", prompts_dir=prompts_dir)
    laws_block = ""
    laws_text = (laws or "").strip()
    if laws_text:
        laws_block = "LAWS OF THE COURT (cite by name when applicable)\n" + laws_text
    return _fill(
        template,
        {
            HOUSE_RULES_SLOT: (house_rules if house_rules is not None else HOUSE_RULES_BLOCK).strip(),
            LAWS_SLOT: laws_block,
            FIGHT_SETUP_SLOT: fight_setup or "(not provided)",
            RECEIPTS_SLOT: receipts or "(none)",
            TRANSCRIPT_SLOT: transcript or "(none — instant / CLI path)",
            EXHIBIT_LEDGER_SLOT: exhibit_ledger or "(none)",
            PRIOR_RULING_SLOT: prior_ruling or "(n/a)",
        },
    )


def render_balance_prompt(
    *,
    matchup: str,
    context: str | None = None,
    prompts_dir: Path | None = None,
) -> str:
    """Render prompts/balance.md for a proposed matchup."""
    template = load_prompt("balance.md", prompts_dir=prompts_dir)
    return _fill(
        template,
        {
            MATCHUP_SLOT: matchup.strip() or "(unspecified)",
            CONTEXT_SLOT: (context or "").strip() or "(none)",
        },
    )
