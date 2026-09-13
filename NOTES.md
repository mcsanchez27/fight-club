# NOTES — ideas not implemented / non-obvious choices

Per brief: extra ideas live here only. Do not change House Rules tone.

## Phase 3 implementation notes

1. **Citations `kind` column** — Single `citations` table with
   `kind` (`receipt`|`exhibit`) rather than a separate `exhibits` table.
   Same shape (claim, url, locator, snippet, verified, retrieved_at); keeps
   queries and `/export` simple. Exhibits are user-pasted; receipts are
   autonomous allowlisted fetches.

2. **Franchise detection** — Optional `/fight franchise:` slash field, else
   word-boundary alias match against `config/sources.json`. Unlisted → no
   retrieval → legal-plea path (confidence ≤5, voided). Only
   `retrieval_status=unavailable` is queued for automatic re-judge.

3. **Stdlib fetch only** — `urllib` + MediaWiki API for fandom/gateway wikis;
   best-effort HTML text extract for Kanzenshuu. No BeautifulSoup / httpx.
   If HTML quality is poor, consider adding `beautifulsoup4` later — not
   required for Phase 3.

4. **Rejudge loop** — `discord.ext.tasks` every 15 minutes + processes on
   cog start after ready. Re-fetches; if status becomes `ok`, re-judges and
   posts into the original channel when possible.

5. **Cost accounting** — Estimated, not billed: ~2.5k in / 1k out without
   retrieval; ~5k in / 1k out with (retrieval ~doubles input). Rates via
   `FIGHT_USD_PER_MTOK_*`. Monthly hard stop is ephemeral.

6. **Stale receipts** — `stale_days` (90) in config; embed/export treat old
   `retrieved_at` as unverified when checked (refresh on re-judge).

## Earlier notes (still open / deferred)

7. **OpenAI tool-use parity** — Anthropic uses `deliver_verdict` tool use;
   OpenAI fallback still uses `response_format=json_object`.

8. **Cooldown on judge failure** — `record()` runs only after a successful
   judge so failed `/fight` / Challenge calls do not burn cooldown or cap.

9. **UTC vs America/Denver for daily cap** — day key uses `date.today()` in
   the host environment.

10. **`/docket remove` / claim / run** — only add/list were requested.

11. **Remove OpenAI dependency** — remains as fallback; not removed.

## V2 item 1 (latency) — Sept 13 2026

12. **Anthropic call shape** — Inspected SDK 0.125: `messages.create` + `input_schema` tool + `tool_choice={"type":"tool","name":...}` still valid. Added optional `"type":"custom"` on the tool dict for forward compatibility. Client now `timeout=60, max_retries=1`. Ruling model via `FIGHT_MODEL_RULING` (default `claude-sonnet-5`) with `ANTHROPIC_MODEL` as V1 fallback.

13. **usage_events timings** — Additive columns only (`retrieval_seconds`, `judge_seconds`, `total_seconds`). Full V2 `fights` schema / `fight_id` / balance role deferred to item 2–3. Real `input_tokens`/`output_tokens` from Anthropic `response.usage` when present; flat estimates remain the fallback.

14. **Retrieval parallelism** — Query×source jobs share one `ThreadPoolExecutor`; executor `shutdown(wait=False, cancel_futures=True)` so the 10s wall clock does not wait on stragglers. In-flight urllib calls are not hard-killed (stdlib limitation).

## V2 item 2 (schema migration) — Sept 13 2026

15. **schema_version = 2** — Additive only. V1 `citations` table kept (Phase 3 receipts/exhibits-as-citations); new V2 `exhibits` table is separate. `usage_events` keeps column names `tokens_in`/`tokens_out` (not renamed to input/output) so item 1 booking stays intact; `fight_id` + `role` added nullable.

16. **Out of scope this commit** — state machine, commands, prompt templates, Discord wiring (items 3–13). Gallery `source_role` column exists; no gallery rows created yet (C4).

## V2 item 3 (prompts + balance) — Sept 13 2026

17. **Prompt templates on disk** — `prompts/referee.md` + `prompts/balance.md`
    loaded via `bot/prompts.py` (`{{HOUSE_RULES}}` / `{{LAWS}}` / fight slots).
    Missing file raises `PromptTemplateError` with the expected path.

18. **deliver_verdict soft migration** — Tool schema is V2 field order with
    `winner_side`; opening/close/argument_quality nullable (Amendment 11).
    `validate_verdict` still accepts V1 CLI payloads (`matchup` + `winner`) and
    maps `winner_side` ↔ `winner` so instant/CLI stays green.

19. **balance_read** — Haiku tool + `balance_read()` / `judge_balance` helper
    (mockable client). Franchise keys normalized through alias map after return
    (Q5); unmapped/empty → that side unlisted for later retrieval routing.
    Discord challenge-card wiring deferred to item 4c.

20. **Out of scope this commit** — challenge card / state machine UX (4a–4c),
    retrieve-at-accept (5), transcript assembly, ruling drop (7–8).

## V2 item 4a (challenge card) — Sept 13 2026

21. **`expire_due_fights(now)`** — pure helper over fight rows (C3 / amendment 9).
    Called at `/fight` and Accept/Decline entry; optional `tasks.loop` left for
    later when the bot runs. Tests freeze the clock — no Discord loop.

22. **`/fight` fields** — V2 optional `opponent` / `matchup` / `context` / `side` /
    `instant` plus V1 `fighter_a`/`fighter_b` bridge for instant. Missing bits →
    ephemeral text only (no menus, amendment 12). Open-ended (side without
    matchup) prompts toward 4b.

23. **Accept** sets `accepted_at` and status `arguing` (accepted→arguing hop).
    Thin `create_thread` stub only; receipts-at-accept is item 5. Counter button
    omitted until 4b. Balance warning / free counter is 4c.

24. **Out of scope this commit** — 4b Counter+modals+open-ended accept; 4c
    balance warning+free counter; item 5 thread+receipts; lead locks A1.
