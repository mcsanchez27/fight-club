# Fight Club

AI court Discord bot that rules fiction / death-battle matchups with receipts.

Lean stack: `discord.py`, Anthropic (preferred) or OpenAI-compatible chat completions, stdlib SQLite for court records, stdlib `urllib` for allowlisted wiki fetch. No web UI. No Notion.

**V2 — the referee.** The bot mostly *observes* advocates argue in a thread, then rules on the record when they rest. Solo / instant judging from V1 still works.

## V2 loop

1. **`/fight`** → challenge card with **Accept / Decline / Counter** (optional open-ended: you name only your side; challengee picks their champion on Accept).
2. **Accept** → public thread under the card; advocates argue; gallery is ignored by the referee.
3. Each advocate **`/rest`** (after the first rest, the other side has a timeout before the court rules on what's there).
4. Referee rules on the transcript snapshot + receipts. Full verdict in the thread; one-line win post in the channel with a jump link.

`instant:true` on `/fight` keeps the **V1 path**: no card/thread — judge immediately (solo play and judge testing). Challenge button on those rulings only; thread fights use `/reconsider` instead.

Standing rule: **never start the Discord bot unless Matt says "start the bot."** CLI + pytest are the test surface. Deadlines are pure functions over fight rows (swept at slash/button entry **and** on the background `tasks.loop` every `sweep_interval_minutes`, default 2); the live loop only runs when the bot is actually up.

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
4. **Privileged Message Content intent** (amendment 13 / C7) — required for thread transcripts:
   1. Developer Portal → your app → **Bot** → **Privileged Gateway Intents** → enable **Message Content Intent**.
   2. In code, `Intents.message_content = True` (see `bot/__main__.py`). Portal toggle alone is not enough.
   3. Startup logs a **warning** if Message Content is missing. Transcript reading needs it; without it, history can look empty even when advocates posted (distinct from a real thin record).
5. OAuth2 → URL Generator:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions (minimum for V2):
     - **Send Messages**, **Embed Links**, **Use Slash Commands**
     - **Create Public Threads**, **Send Messages in Threads**
     - **Read Message History** (transcripts)
     - **Add Reactions** (❌ contest on exhibits)
     - **Manage Threads** (archive after ruling)
     - (or Administrator for testing)
6. Open the generated invite URL, add the bot to your server.

### Environment

| Variable | Required | Notes |
|----------|----------|-------|
| `ANTHROPIC_API_KEY` | preferred (CLI + bot) | Anthropic API key — used when set |
| `ANTHROPIC_MODEL` | no | V1 fallback for ruling model if `FIGHT_MODEL_RULING` unset |
| `FIGHT_MODEL_RULING` | no | default `claude-sonnet-5` |
| `FIGHT_MODEL_BALANCE` | no | default `claude-haiku-4-5-20251001` |
| `OPENAI_API_KEY` | fallback | OpenAI-compatible key if Anthropic unset |
| `OPENAI_BASE_URL` | no | OpenAI-compatible base URL |
| `OPENAI_MODEL` | no | default `gpt-4o-mini` |
| `DISCORD_TOKEN` | bot only | Bot token |
| `DISCORD_APPLICATION_ID` | optional | handy for invite docs |
| `FIGHT_COOLDOWN_SECONDS` | no | per-user cooldown (default `60`) — also §7 `cooldown_seconds` |
| `FIGHT_GUILD_DAILY_CAP` | no | max judge calls / guild / UTC day (default `50`) — §7 `daily_cap` |
| `FIGHT_MONTHLY_USD_CAP` | no | estimated spend hard stop (default `20`) — §7 `monthly_usd_cap` |
| `FIGHT_TOKEN_CEILING` | no | per-ruling in+out token estimate ceiling (default `16000`) |
| `FIGHT_MAX_OUTPUT_TOKENS` | no | max output tokens per ruling (default `4096`, floor `1024`) — below ~2048 the verdict truncates and citations are dropped |
| `FIGHT_RETRIEVAL_ENABLED` | no | `1`/`0` — disable autonomous wiki fetch (default `1`) |
| `FIGHT_RETRIEVAL_BUDGET_SECONDS` | no | global wall-clock for parallel wiki fetch (default `10`) |
| `FIGHT_USD_PER_MTOK_INPUT` | no | estimate $/M input (default `2.0` — Sonnet 5) |
| `FIGHT_USD_PER_MTOK_OUTPUT` | no | estimate $/M output (default `10.0` — Sonnet 5) |
| `FIGHT_CHALLENGE_TIMEOUT_HOURS` | no | challenge card expiry (default `6`) |
| `FIGHT_COUNTERS_PER_SIDE` | no | counters before void (default `2`) |
| `FIGHT_BALANCE_WARN_BELOW` | no | warn when balance score **strictly below** this (default `4`; 10 = even) |
| `FIGHT_BALANCE_FREE_COUNTER` | no | free Counter when balance-warned (default `1` / on) |
| `FIGHT_REST_TIMEOUT_HOURS` | no | after first `/rest` (default `24`) |
| `FIGHT_TRANSCRIPT_MAX_TOKENS` | no | judge-pack transcript cap (default `20000`) |
| `FIGHT_THREAD_ARCHIVE_DELAY_HOURS` | no | archive delay after `ruled_at` (default `24`) |
| `FIGHT_ALLOWED_CHANNELS` | no | comma-separated channel IDs; empty = all |
| `FIGHT_SWEEP_INTERVAL_MINUTES` | no | background deadline sweep interval (default `2`) — §7 `sweep_interval_minutes` |

Env vars are the **global default**. Per-server `guild_config` (via `/config`) overrides them. Hardcoded §7 defaults sit under both.

### Guild config keys (§7)

Writable with `/config` (`Manage Server`). Precedence: **env default &lt; guild override**.

| Key | Default | Meaning |
|-----|---------|---------|
| `counters_per_side` | `2` | Max counters per advocate side before void |
| `challenge_timeout_hours` | `6` | Unanswered challenge → expired |
| `rest_timeout_hours` | `24` | After first `/rest`, other side's deadline |
| `balance_warn_below` | `4` | Show ⚖️ Referee's read when score &lt; threshold |
| `balance_free_counter` | `true` | Balance-warned Counter does not consume quota |
| `cooldown_seconds` | `60` | Per-user cooldown between judge calls |
| `daily_cap` | `50` | Per-guild judge calls per UTC day |
| `monthly_usd_cap` | `20` | Estimated spend hard stop |
| `allowed_channels` | `[]` | Empty = all channels; else only listed IDs |
| `thread_archive_delay_hours` | `24` | Archive fight thread after ruling |
| `transcript_max_tokens` | `20000` | Middle-truncation cap for the judge pack |
| `sweep_interval_minutes` | `2` | Background loop: deadline sweep (+ rejudge queue) |

## Run

### CLI (no Discord token needed)

```bash
source .venv/bin/activate
python -m bot.cli "Goku vs Superman"
python -m bot.cli "Batman vs Iron Man" -c "no prep, random alley"
```

Anthropic is preferred when `ANTHROPIC_API_KEY` is set; OpenAI still works via `OPENAI_API_KEY`. If neither key is set, the CLI prints a clear error and exits non-zero.

### Discord bot

Only when Matt says **"start the bot"**:

```bash
source .venv/bin/activate
python -m bot
```

## Commands

### V2 fight loop

- **`/fight`** — every field optional:
  - `opponent`, `matchup`, `context`, `side`, `instant`
  - V1 bridge for instant: `fighter_a`, `fighter_b`, `franchise`, `exhibits`
  - Missing bits → ephemeral text prompt (no menus in V2)
  - Open-ended: `opponent` + `side` without `matchup` → challengee names champion on Accept
  - `instant:true` → skip card/thread, rule immediately (V1 path on a fight row)
- **Accept / Decline / Counter** — buttons on the challenge card (persistent view). Counter opens a modal (matchup / context / swap sides). Open-ended Accept opens a champion modal. Balance warning shows as **⚖️ Referee's read** and may label **Counter (free)**.
- **`/rest`** — advocate + in-thread. First rest starts the rest deadline; second (or timeout) → judge path.
- **`/forfeit`** — advocate + in-thread; Confirm button → L for you, W for the other.
- **`/cancel`** — advocate + in-thread handshake; both must run it → voided (no record).
- **`/reconsider evidence:…`** — advocate + in-thread, **once** per ruled fight; non-empty evidence required. Re-rules from the stored transcript snapshot + prior ruling.
- **`/leaderboard`** — server top 15 by W, then win%, streak column (derived; voided/expired/instant excluded).
- **`/record [user]`** — W-L, current streak, last 5.
- **`/config [key] [value]`** — `Manage Server` only. No args lists effective values; set writes `guild_config`.
- **❌ reaction** — opposing advocate on a quote/link message → exhibits on that message marked `contested` (no new command).

### Kept from V1

- **`/standings [limit]`** — last N rulings in this server
- **`/laws`** — Laws of the Court (`laws.md`, also injected into the judge system prompt)
- **`/docket add` / `/docket list`** — bank matchups per server in SQLite
- **`/export [message_id]`** — markdown block of a ruling for paste-anywhere (ephemeral)
- **Challenge** button — **instant rulings only** (thread fights → `/reconsider`)

## Receipts (Phase 3 + V2)

- Allowlist per franchise in `config/sources.json` (Dragon Ball, ASOIAF, LotR, Vikings). Hard no: VS Battles, Reddit, YouTube, power-scaling tier lists.
- V2: receipts fetched at **Accept** (keyed by fight), reused at ruling; one short refresh if empty/unavailable. Unlisted / fetch failure → court **still rules**, confidence capped, gaps pleaded (House Rule 3).
- Snippets capped at **25 words**; links + locators stored; no full quotes. Quotes in-thread are usually **unverified** unless they hit a fetched receipt; ❌ contest is the real lever.
- Images acknowledged on the exhibit ledger only — never verified, never weighed alone.

### Cost estimate

Retrieval roughly **doubles input tokens**. Ballpark Sonnet 5 at \$2/\$10 per MTok:

| Mode | Tokens (in/out) | Est. $ / fight |
|------|-----------------|----------------|
| Instant / no retrieval | ~2.5k / 1k | ~$0.015 |
| Thread fight + retrieval | ~5–8¢ class | see usage logs |

Default monthly cap **$20** ≈ hundreds of fights before hard stop (guild daily cap still applies). See `docs/receipts-design.md`. Real token + stage timings land in `usage_events`.

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
  __main__.py      # python -m bot (Message Content intent + startup warn)
  cli.py           # python -m bot.cli "A vs B"
  commands.py      # slash commands + challenge card + views
  fights.py        # state machine helpers, Accept/Counter/rest/cancel
  ruling.py        # judge_ready → deliver_verdict, reconsider, instant
  records.py       # derived W/L, leaderboard, flare
  transcript.py    # snapshot assembly + middle truncation
  exhibits.py      # extract / verify / ❌ contest
  config.py        # §7 resolver (env < guild_config)
  prompts.py       # load prompts/*.md templates
  judge.py         # tools + validate_verdict + balance_read
  retrieval.py     # allowlisted urllib fetch
  sources.py       # config/sources.json allowlist + hard-nos
  embeds.py        # Discord embed formatter
  export.py        # /export markdown
  budget.py        # monthly $ + token ceiling
  limits.py        # cooldown / daily cap
  progress.py      # deferred interaction progress edits
  db.py            # SQLite court.db
  laws.py          # laws.md loader
config/
  sources.json     # franchise allowlist
prompts/
  referee.md       # ruling system template
  balance.md       # balance_read template
docs/
  receipts-design.md
laws.md
data/
  court.db         # created at runtime
```

## Tests

```bash
source .venv/bin/activate
pytest -q
```

No Discord token / live Anthropic calls required — judge and Discord are mocked.

## V2 build

Local commits on `main` from `165a520` (Round 3 receipts). **Cloud Agents unavailable** — built locally on the box. One commit per §11 item (4 split as 4a/4b/4c):

| Commit | Item | Summary |
|--------|------|---------|
| `2ea808b` | **1** | Latency budget, Anthropic client timeouts, real token timings |
| `455d14a` | **2** | `schema_version` + V2 fights/exhibits/guild_config tables |
| `45b6c60` | **3** | Prompt templates + `balance_read` tool + model routing |
| `3bf5e93` | **4a** | Challenge card Accept/Decline + expiry sweep |
| `f8220d6` | **4b** | Counter modal + open-ended accept |
| `d7c1fa5` | **4c** | Balance warning chrome + free counter |
| `a28c048` | **5** | Thread on Accept + fight-keyed receipts under 10s budget |
| `63507d8` | **6** | `/rest` `/forfeit` `/cancel` + `judge_due_rests` → `judge_ready` once |
| `caaf25e` | **7** | Transcript snapshot + exhibit extract/verify + ❌ contest |
| `8937708` | **8** | `judge_ready` → `deliver_verdict` ruling drop + `archive_due` |
| `3bb0f7a` | **9** | Derived records, `/leaderboard`, `/record`, ruling flare line |
| `f94bcb8` | **10** | `/reconsider` once-per-fight with snapshot reuse + parent link |
| `b42fce0` | **11** | `/config` + guild overrides (§7) with env &lt; guild precedence |
| `b8c479a` | **12** | `instant:true` on fight rows; Challenge limited to instant rulings |
| *(this)* | **13** | README: commands, config, intents/permissions, this session report |

Pytest count at end of item **12**: **182**. After this README (+ Message Content intent wiring) change: **`182 passed`** (`pytest -q`).

**Out of scope (not built):** guided setup / menus · points · titles · server-wide flare (roles, nicknames) · gallery polls · seasons · model A/B / OpenRouter / `/rejudge` · live per-message refereeing · Notion integration · admin panel UI.
