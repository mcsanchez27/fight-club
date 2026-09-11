# Fight Club

AI court Discord bot that rules fiction / death-battle matchups with receipts.

Lean stack: `discord.py`, Anthropic (preferred) or OpenAI-compatible chat completions, in-memory challenge state. No database, no web UI.

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

Then in Discord: `/fight fighter_a:… fighter_b:… context:…` (context optional).  
Use the **Challenge** button on a ruling to submit new evidence; the court re-judges with the prior verdict plus your challenge (last verdict stored per ruling message id in memory).

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
  judge.py      # system prompt + JSON verdict
  embeds.py     # Discord embed formatter
  commands.py   # /fight + Challenge view
```
