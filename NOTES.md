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
