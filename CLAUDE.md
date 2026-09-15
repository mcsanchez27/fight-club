# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Standing rule

**Never start the Discord bot unless Matt says "start the bot."** The CLI and pytest are the
test surface — both run without a Discord token and without live model calls.

## Commands

```bash
# venv (POSIX)
source .venv/bin/activate
# venv (Windows / Git Bash)
./.venv/Scripts/python.exe -m pytest -q

pytest -q                                          # full suite
pytest tests/test_item8_ruling.py -q                # one file
pytest tests/test_item8_ruling.py::test_name -q     # one test
pytest -q -k reconsider                             # by keyword

python -m bot.cli "Goku vs Superman"               # solo judge, no Discord
python -m bot.cli "Batman vs Iron Man" -c "no prep, random alley"
python -m bot                                       # the bot — only on explicit say-so
```

`.env` is required for CLI runs (`ANTHROPIC_API_KEY` preferred, `OPENAI_API_KEY` as fallback);
copy from `.env.example`. Tests need neither.

## Architecture

A Discord bot that rules fiction matchups. Two paths share one judge:

- **V2 (thread fights)** — `/fight` posts a challenge card → Accept opens a public thread →
  advocates argue → each `/rest` → referee rules on a **transcript snapshot**, not live messages.
  The gallery is ignored; only the two advocates are on the record.
- **V1 (`instant:true`)** — no card, no thread, judge immediately. Still writes a `fights` row
  (`instant=1`), so records and receipts stay uniform.

Flow of control for a thread fight: `commands.py` (slash + views) → `fights.py` (state
transitions) → `transcript.py` + `exhibits.py` (build the record) → `judge.py` (model call) →
`ruling.py` (verdict → embed → archive) → `records.py` (derived W/L).

### Fight state machine (`bot/fights.py`)

`proposed` → `arguing` → `resting` → `judge_ready` → `ruled`, plus terminal `expired` / `voided`.
Transitions raise `ValueError` on illegal status rather than silently no-op'ing — preserve that.

**Deadlines are pure functions over fight rows**, not timers: `expire_due_fights`,
`judge_due_rests`, `archive_due`, aggregated by `sweep_deadlines(db, now)`. `now` is always
injected, never read inside. Sweeps run at **two** entry points — at the top of slash/button
handlers *and* on the background `tasks.loop` every `sweep_interval_minutes` (default 2). This is
why the suite can test every deadline without the bot running.

### Judge (`bot/judge.py`)

Anthropic tool-use is the real path: `deliver_verdict` (ruling, Sonnet 5) and `balance_read`
(pre-fight balance check, Haiku 4.5), both via `tool_choice={"type":"tool","name":...}`.
OpenAI is a fallback using `response_format=json_object`, not tool use — the two paths are not
at parity and payloads diverge.

**Anthropic `messages.create` must not pass `temperature` / `top_p` / `top_k`** — Sonnet 5
returns 400 for non-default sampling. The OpenAI JSON path still sets `temperature=0.4`. Don't
"restore" sampling params to the Anthropic calls.

`validate_verdict` normalizes and is the gate — model output is never trusted raw.
`apply_retrieval_guardrails` caps confidence when receipts are thin (House Rule 3: a court that
can't verify still rules, it just pleads the gap).

Field order in the verdict schema is deliberate: **steelman before ruling**, so the model commits
to the opposing case before it decides. Reordering changes behavior.

`laws.md` is injected into the judge system prompt and is user-visible via `/laws`.

### Config (`bot/config.py`)

Precedence: **hardcoded default < env var < `guild_config` row**. `CONFIG_KEYS` is the single
source of truth — key name, default, env var, and type tag all live in that one dict. Adding a
`/config` key means adding one entry there, not touching the command.

`sweep_interval_minutes` is the one key a single guild cannot own: one background loop serves
every guild. `loop_sweep_interval(db)` reconciles it by ticking at the **tightest** interval any
guild asked for — sweeping early is free, since deadlines are pure functions and an empty sweep
is a no-op. Resolve it with `loop_sweep_interval`, not `get_guild_config(..., guild_id=None)`.

### Storage (`bot/db.py`)

Stdlib `sqlite3`, `data/court.db`, created at runtime. **Migrations are additive only**, guarded
by `schema_version` (currently 2). V1 rows must keep reading through V1 accessors after migration.

Naming trap: the V1 `citations` table has a `kind` column (`receipt` | `exhibit`), *and* V2 added
a separate `exhibits` table. They are different things. Phase 3 receipts/exhibits live in
`citations`; V2 thread exhibits live in `exhibits`. Check which one a function means.

Likewise `usage_events` keeps V1 column names `tokens_in` / `tokens_out` — deliberately not
renamed to input/output, so item-1 cost booking stays intact.

### Retrieval (`bot/retrieval.py`, `bot/sources.py`)

Stdlib `urllib` + MediaWiki API only — **no `requests`, `httpx`, or `beautifulsoup4`** by design.
Allowlist per franchise in `config/sources.json`; VS Battles, Reddit, YouTube, and power-scaling
tier lists are hard-nos. Fetches are parallel under a global wall-clock budget
(`FIGHT_RETRIEVAL_BUDGET_SECONDS`, default 10) with `cancel_futures=True`; stragglers are
abandoned, not awaited. Unlisted franchise or failed fetch → still rule, cap confidence.

Snippets are capped at 25 words and store links + locators, never full quotes.

## Conventions

- **`NOTES.md` is the running log.** Non-obvious choices, deferred work, and known gaps go there
  as numbered entries under a dated section — not as code comments. Read the tail before starting;
  append when you make a judgment call worth explaining. Existing entries are append-only history.
- **House Rules tone is fixed.** Don't rewrite the voice in `README.md` house rules or `laws.md`.
- **Commits are one-per-item**, prefixed with the item number (`11: /config + guild overrides`).
  Follow-ups use `F1` / `F2` / `F3`.
- **Tests inject, don't patch the network.** The judge is passed in as `judge_fn=lambda **kw: ...`
  and Discord objects are `MagicMock` / `AsyncMock`; `monkeypatch` is for env vars and
  `bot.commands.get_db`. There is no `conftest.py` — fixtures are local to each test file.
- Records exclude `voided`, `expired`, and `instant` fights. Challenge buttons attach to instant
  rulings only; thread fights use `/reconsider` (once per fight).
