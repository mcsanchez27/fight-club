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
    Thin `create_thread` stub only; receipts-at-accept is item 5. Counter +
    open-ended landed in 4b. Balance warning / free counter is 4c.

24. **Out of scope in 4a** — completed by 4b/pending 4c/5 as noted below.

## V2 item 4b (Counter + open-ended accept) — Sept 13 2026

25. **Counter matrix (Q1)** — Always flips button holder (`challenger_id` ↔
    `challengee_id`). Optional swap flips `side_a`↔`side_b` **and**
    `advocate_a`↔`advocate_b`. Matchup/context overwrite only when non-empty.
    Increments the countering advocate's pre-swap side counter; resets
    `expires_at`; stays `proposed`. At `counters_per_side` (default 2) →
    `voided`.

26. **Open-ended** — `/fight opponent + side` without matchup posts an
    open-ended card (`side_b` empty). Accept opens a modal for the challengee's
    champion (+ optional context). Balance is deferred until both sides are
    known (A1); `apply_balance_to_fight` stores scores/franchises but does **not**
    post warning chrome (4c).

27. **4c hooks** — implemented in item 4c (`counter_button_label`,
    `is_balance_free_counter_eligible`, `balance_warning_field`,
    `count_against_limit=` / auto-detect on `counter_fight`).

28. **Out of scope in 4b** — completed by 4c / pending item 5 as noted below.

## V2 item 4c (balance warning + free counter) — Sept 13 2026

29. **Threshold comparison** — `balance_warn_below` (guild_config, else
    `FIGHT_BALANCE_WARN_BELOW`, else **4** from §7). Score is 1–10 with
    **10 = even**. Warning when ``score < threshold`` (strict). Score **equal**
    to the threshold is *not* a warning (even enough). Documented because
    "below" vs "at or below" was ambiguous.

30. **When balance runs** — only after both sides are known (lead lock A1).
    Normal `/fight` matchup: at submit, then warning chrome on the public card.
    Open-ended: deferred until Accept modal fills the champion, before the
    thread opens. No one-sided score on the card. `apply_balance_to_fight`
    stores `balance_score` / `balance_favored` / `balance_reason` /
    `balance_warned` plus franchise keys via `normalize_franchise_key`.

31. **Warning chrome** — field name `⚖️ Referee's read`, value
    `{side} favored (N/10) — reason`. Labeled as the referee's read; never
    a ruling. Hidden unless `balance_warned` and sides are complete.

32. **Free counter** — when `balance_warned` and `balance_free_counter`
    (guild_config / `FIGHT_BALANCE_FREE_COUNTER`, default **on**): button
    label `Counter (free)` and `count_against_limit=False` so `counters_*`
    do not increment. While still warned every such counter is free (no
    one-shot column; a `free_counter_used` flag would need schema). Disable
    per guild with `balance_free_counter=0`.

33. **`underdog_accepted`** — set on Accept when the fight is
    `balance_warned` and the accepting actor is the advocate of the
    *unfavored* side (`balance_favored` is the favorite). Not a ruling.

34. **Out of scope in 4c** — completed by item 5 / pending rest+judge as noted below.

## V2 item 5 (thread + receipts at accept) — Sept 13 2026

35. **`receipts` table** — Additive fight-keyed storage for accept-time passages
    (`fight_id, claim, source_url, locator, snippet, verified, retrieved_at,
    kind, source_title, retrieval_id, franchise, side`). V1 `citations` stays
    ruling-keyed. `schema_version` remains **2** (CREATE IF NOT EXISTS only).

36. **Accept path** — progress "Retrieving receipts…" → balance (if sides
    known) → `retrieve_for_accept` under 10s budget using `franchise_a` /
    `franchise_b` → store passages + `fights.retrieval_status` → public thread
    under the card with opening message (matchup, sides, context, rules,
    "each side `/rest` when done") → status `arguing` + `accepted_at` +
    `thread_id`. **Accept always succeeds** (C2); gap note when empty /
    unavailable. No gallery exhibit rows (C4).

37. **Dual franchise** — same key → one `retrieve()`; distinct keys → sequential
    per-side retrieves sharing the wall clock (each call still parallelizes
    sources). Unmapped both → `unlisted`.

38. **Out of scope in item 5** — completed by item 6 / pending 7–8 as noted below.

## V2 item 6 (rest / forfeit / cancel) — Sept 13 2026

39. **Judge-ready signal for item 8** — status becomes **`judge_ready`**
    (constant `JUDGE_READY_STATUS`). No Sonnet / `judge()` call in item 6.
    `mark_judge_ready(db, fight_id)` is idempotent (returns True only on the
    first transition) so A3 near-simultaneous `/rest` cannot double-invoke.
    Item 8 should consume rows with `status='judge_ready'`.

40. **`judge_due_rests(now)`** — pure helper alongside `expire_due_fights`
    (amendment 9). Resting fights with `rest_deadline_at <= now` →
    `judge_ready` once. `sweep_deadlines` runs both at `/rest` `/forfeit`
    `/cancel` and at challenge-card / `/fight` entry. Tests freeze the clock;
    no `tasks.loop` required.

41. **`/rest`** — advocate + in-thread only. First rest → `resting`, set
    `rest_a_at`/`rest_b_at`, `rest_deadline_at = now + rest_timeout` (default
    24h via `FIGHT_REST_TIMEOUT_HOURS` / guild_config). Second rest (other
    side) → `judge_ready` once. Empty rest allowed (amendment 7); thin-record
    banner is item 8.

42. **`/forfeit`** — advocate + in-thread; ephemeral Confirm button →
    `forfeited`. No `fights.winner_*` columns (item 2); item 9 derives the L.

43. **`/cancel`** — advocate + in-thread handshake via `cancel_requested_by`;
    other side confirms → `voided`.

44. **Out of scope this commit** — transcript / exhibits / contest (7);
    ruling call/drop / thin-record banner (8); records derivation (9).
