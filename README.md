# Fight Club

AI court Discord bot that rules fiction / death-battle matchups with receipts.

Lean stack: `discord.py`, Anthropic (preferred) or OpenAI-compatible chat completions, stdlib SQLite for court records, stdlib `urllib` for allowlisted wiki fetch. No web UI. No Notion.

## Setup

```bash
cd fight-club
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — at minimum ANTHROPIC_API_KEY (preferred) or OPENAI_API_KEY; add Discord vars to run the bot
```

### Discord application

1. Create an app at [Discord Developer Portal](https://discord.com/developers/applications).
2. **Bot** tab → Add Bot → copy the token → `DISCORD_TOKEN`.
3. **General Information** → Application ID → `DISCORD_APPLICATION_ID`.
4. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Send Messages`, `Embed Links`, `Use Slash Commands` (or Administrator for testing)
5. Open the generated invite URL, add the bot to your server.

### Environment

| Variable | Required | Notes |
|----------|----------|-------|
| `ANTHROPIC_API_KEY` | preferred (CLI + bot) | Anthropic API key — used when set |
| `ANTHROPIC_MODEL` | no | default `claude-sonnet-4-6` |
| `OPENAI_API_KEY` | fallback | OpenAI-compatible key if Anthropic unset |
| `OPENAI_BASE_URL` | no | OpenAI-compatible base URL |
| `OPENAI_MODEL` | no | default `gpt-4o-mini` |
| `DISCORD_TOKEN` | bot only | Bot token |
| `DISCORD_APPLICATION_ID` | optional | handy for invite docs |
| `FIGHT_COOLDOWN_SECONDS` | no | per-user cooldown between judge calls (default `60`) |
| `FIGHT_GUILD_DAILY_CAP` | no | max judge calls per guild per UTC day (default `50`) |
| `FIGHT_MONTHLY_USD_CAP` | no | estimated spend hard stop (default `20`) |
| `FIGHT_TOKEN_CEILING` | no | per-ruling in+out token estimate ceiling (default `16000`) |
| `FIGHT_RETRIEVAL_ENABLED` | no | `1`/`0` — disable autonomous wiki fetch (default `1`) |
| `FIGHT_USD_PER_MTOK_INPUT` | no | estimate rate $/M input tokens (default `3.0`) |
| `FIGHT_USD_PER_MTOK_OUTPUT` | no | estimate rate $/M output tokens (default `15.0`) |

## Run

### CLI (no Discord token needed)

```bash
source .venv/bin/activate
python -m bot.cli "Goku vs Superman"
python -m bot.cli "Batman vs Iron Man" -c "no prep, random alley"
```

Anthropic is preferred when `ANTHROPIC_API_KEY` is set; OpenAI still works via `OPENAI_API_KEY`. If neither key is set, the CLI prints a clear error and exits non-zero.

### Discord bot

```bash
source .venv/bin/activate
python -m bot
```

Then in Discord:
- `/fight fighter_a:… fighter_b:… [context] [franchise] [exhibits]` — exhibits are user-pasted **EXHIBITS**; autonomous wiki fetch produces **RECEIPTS** when the franchise maps in `config/sources.json`
- `/export [message_id]` — markdown block of a ruling for paste-anywhere (ephemeral)
- `/standings [limit]` — last N rulings in this server
- `/laws` — Laws of the Court (from `laws.md`, also injected into the judge system prompt)
- `/docket add` / `/docket list` — bank matchups per server in SQLite

Use the **Challenge** button on a ruling to submit new evidence (treated as an exhibit); the court re-judges with the prior verdict plus your challenge.

## Receipts (Phase 3)

- Allowlist per franchise in `config/sources.json` (Dragon Ball, ASOIAF, LotR, Vikings). Hard no: VS Battles, Reddit, YouTube, power-scaling tier lists.
- Unlisted franchise or fetch failure → court **still rules**, embed banner `unverified: retrieval unavailable`, confidence capped at **5/10**, ruling voided and queued for automatic re-judge when retrieval returns (House Rule 3).
- Snippets capped at **25 words**; links + locators stored; no full quotes. Receipts older than 90 days are stale.
- SQLite `citations` table with `kind` = `receipt` | `exhibit`.

### Cost estimate

Retrieval roughly **doubles input tokens**. Ballpark Sonnet-class:

| Mode | Tokens (in/out) | Est. $ / fight |
|------|-----------------|----------------|
| No retrieval | ~2.5k / 1k | ~$0.022 |
| With retrieval | ~5k / 1k | ~$0.030 |

Default monthly cap **$20** ≈ hundreds of fights before hard stop (guild daily cap still applies). See `docs/receipts-design.md`.

## House rules

1. Steelman first  
2. Concede what's earned  
3. Canon citations beat vibes; "I don't know that material" is a legal plea, not a loss  
4. Rulings with confidence X/10, revisable on new evidence  
5. Traps are legal  
6. The migraine gets the final say. Court recesses whenever the King calls it  

## Layout

```
bot/
  __init__.py
  __main__.py   # python -m bot
  cli.py        # python -m bot.cli "A vs B"
  judge.py      # system prompt + JSON verdict + receipt guardrails
  retrieval.py  # allowlisted urllib fetch
  sources.py    # config/sources.json allowlist + hard-nos
  embeds.py     # Discord embed formatter
  export.py     # /export markdown
  budget.py     # monthly $ + token ceiling
  commands.py   # slash commands + Challenge view + rejudge loop
  db.py         # SQLite court.db
config/
  sources.json  # franchise allowlist
docs/
  receipts-design.md
```

## Tests

```bash
source .venv/bin/activate
pytest -q
```
