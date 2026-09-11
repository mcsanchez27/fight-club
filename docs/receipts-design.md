# Receipts design (Phase 3)

Fight Club Court already had a `citations` string list on every verdict. That was a
label, not a receipt. Receipts mean a reader can verify the claim without trusting
the model.

## Goal

Every **load-bearing** claim in a ruling or concession should either:

1. Point at a **retrievable source** (URL + locator + ≤25-word snippet), or
2. Be tagged as an **unknown / legal plea** when retrieval comes back empty.

House Rule 3: *"I don't know that material" is a legal plea, not a loss.* Empty
or unavailable retrieval produces that plea — not a hallucinated citation — and
the court **still rules**.

Steelmans may cite optionally; they are not receipt-gated.

---

## LOCKED DECISIONS (Matt)

1. **Allowlist per franchise** in `config/sources.json` (not hardcoded). Seed:
   - Dragon Ball → Dragon Ball Wiki + Kanzenshuu
   - ASOIAF → A Wiki of Ice and Fire
   - LotR → Tolkien Gateway
   - Vikings → Vikings Wiki
   **Hard no:** VS Battles Wiki, Reddit, YouTube, any power-scaling tier list.
   Unlisted franchise → no retrieval → legal-plea path (decision 8).

2. **Receipts vs exhibits**
   - Autonomous fetch → **RECEIPTS** (`kind=receipt`).
   - User-pasted text → **EXHIBITS** (`kind=exhibit`).
   - Exhibit = claim; court tries to verify against allowlist; mark
     verified/unverified. Unverified exhibits can still move a ruling; embeds
     show the flag.

3. **Receipts are load-bearing only** — required for the ruling itself and every
   concession; optional for steelmans. Not every line.

4. **SQLite `citations` table** (real, queryable):
   `id, ruling_id, claim, source_url, locator, snippet, verified, retrieved_at`
   plus `kind` (`receipt`|`exhibit`) — see NOTES.md.

5. **Storage bar:** links + locators + snippet capped at **25 words**. No full
   quotes. Store `retrieved_at`; receipts older than **90 days** are stale.

6. **Discord-only** (no Notion). `/export` produces a markdown block of the
   ruling for paste-anywhere.

7. **Cost brakes:** monthly dollar cap env var default **$20**, hard stop with
   ephemeral. Per-ruling token ceiling too. Guild daily call cap remains.

8. **Never refuse.** If retrieval unavailable/unlisted: still rule, flag embed
   `unverified: retrieval unavailable`, **cap confidence at 5/10**, void ruling
   for automatic re-judge when retrieval is back. House Rule 3.

---

## Retrieval step (before judging)

Pipeline for each `/fight` / Challenge:

1. **Parse query** — fighters, context, franchise (slash field or alias map),
   user exhibits.
2. **Allowlist check** — franchise mapped? If not → retrieval_status=`unlisted`.
3. **Retrieve** — MediaWiki/HTML fetch from allowlisted bases only; reject hard-nos.
4. **Pack context** — RECEIPTS + EXHIBITS into the judge user message; cap tokens.
5. **Judge** — existing `deliver_verdict` tool; load-bearing claims must cite
   receipt/exhibit ids or go in `unknowns`.
6. **Post-process** — persist `citations` rows; if retrieval unavailable, clamp
   confidence ≤ 5 and enqueue re-judge.

Challenge flow reuses the same retrieval; challenge text is also treated as an
exhibit claim.

---

## Citation objects (verdict + DB)

```json
{
  "citations": [
    {
      "claim": "Broly's power rises continuously in combat",
      "source_url": "https://dragonball.fandom.com/wiki/Broly",
      "locator": "Power section",
      "snippet": "His power increases the longer he fights …",
      "verified": true,
      "kind": "receipt",
      "retrieved_at": "2026-09-11T19:00:00Z"
    }
  ]
}
```

Display: verified ✓ / unverified ✗ flags; retrieval-unavailable banner on embed.

---

## Cost and latency (estimates)

Assumptions: Claude Sonnet-class judge (~$3/M input, ~$15/M output — override via
env). Retrieval pack roughly **doubles input tokens** vs Phase 2.

| Piece | Latency | Cost (order of magnitude) |
|---|---|---|
| Wiki fetch (1–3 pages, cached TTL) | 200–800 ms cold; ~0 cached | Bandwidth only |
| Judge call (tool use) | 3–12 s | Dominant |
| Judge + retrieval pack | ~2× input tokens | **~1.5–2× $ per fight** vs no-retrieval |
| Challenge | same as fight | Counts against guild daily + monthly $ |

**Worked example (default caps):**
- Phase-2 fight ≈ 2.5k in + 1k out ≈ **$0.022**
- With retrieval ≈ 5k in + 1k out ≈ **$0.030**
- Monthly $20 ≈ **~650–900 fights** before hard stop (before guild daily cap)

Env: `FIGHT_MONTHLY_USD_CAP` (default `20`), `FIGHT_TOKEN_CEILING` (per-ruling
prompt+completion soft ceiling; hard-stop if estimate exceeds),
`FIGHT_RETRIEVAL_ENABLED` (default `1`).

Caching wiki pages per fighter/title (TTL 24h) keeps repeat matchups cheap.
Guild daily cap remains the primary call-volume brake; monthly $ is the spend
brake.

---

## What "I don't know that material" looks like

When retrieval is empty, failed, or franchise unlisted:

- Embed banner: **unverified: retrieval unavailable**
- `confidence` clamped to **≤ 5/10**
- `unknowns` includes legal pleas for missing canon
- Ruling is **voided** and queued for automatic re-judge when retrieval returns
- Court still delivers a ruling (House Rule 3) — never refuse

Stale receipts (>90 days): treated as unverified until refreshed.

---

## Open questions

None blocking Phase 3. Franchise seed and hard-nos are locked; expand
`config/sources.json` as needed.

---

## Non-goals

- Building a general RAG platform / embeddings index.
- Auto-editing Notion Court Records.
- Trusting model-named URLs without allowlist + retrieval check.
- Full-page quote storage.
