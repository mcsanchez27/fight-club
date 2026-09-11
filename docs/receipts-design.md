# Receipts design (Phase 3 — design only)

Fight Club Court already has a `citations` string list on every verdict. That is a
label, not a receipt. Receipts mean a reader can verify the claim without trusting
the model. This doc proposes how to get there. **No code in this phase.**

## Goal

Every material claim in a steelman or ruling should either:

1. Point at a **retrievable source** (URL + quote/paraphrase + locator), or
2. Be tagged as an **unknown / legal plea** when retrieval comes back empty.

House Rule 3 already says *"I don't know that material" is a legal plea, not a
loss.* Empty retrieval should produce that plea — not a hallucinated citation.

## Candidate sources

| Source class | Examples | Pros | Cons |
|---|---|---|---|
| Fandom / wiki | Dragon Ball Wiki, Marvel Database, Tolkien Gateway, One Piece Wiki | Broad coverage, linkable | Secondary; edit wars; spoilers; ToS / scraping risk |
| Official guides | databooks, artbooks, absolute editions, rulebooks | Higher authority | Paywalled, OCR, citation format messy |
| Primary canon | episode/chapter text where licensed | Best authority | Rights; most titles unavailable as clean text |
| Court-local | `laws.md`, prior `data/court.db` rulings | Free, on-brand | Not external canon |
| User exhibits | paste / attachment on `/fight` or Challenge | Highest trust for that bout | Manual; uneven |

**Recommendation to start:** court-local Laws + prior rulings + **opt-in user
exhibits**, plus a small allowlist of public wiki pages fetched at judge time.
Do not scrape indiscriminately.

## Retrieval step (before judging)

Proposed pipeline for each `/fight` / Challenge:

1. **Parse query** — fighters, context, any user-supplied exhibits.
2. **Retrieve** — top-k passages from (a) laws.md, (b) recent guild rulings,
   (c) allowlisted wiki pages for the fighters, (d) user exhibits.
3. **Pack context** — attach retrieved passages to the judge user message (or a
   `consult_sources` tool later). Cap tokens.
4. **Judge** — existing `deliver_verdict` tool; extend citation objects (below).
5. **Validate** — citations must reference retrieval IDs that actually appeared
   in the packed context. Drop or flag orphans.

Challenge flow reuses the same retrieval with the challenge text as an extra
query.

## Citation storage and display

Replace free-string citations with structured objects (schema sketch):

```json
{
  "citations": [
    {
      "id": "src_12",
      "claim": "Broly's power rises continuously in combat",
      "source_title": "Dragon Ball Super manga",
      "locator": "ch. 38",
      "url": "https://…",
      "quote": "optional short quote",
      "retrieval_id": "ret_7",
      "confidence": "high|medium|low"
    }
  ]
}
```

**SQLite:** store the verdict JSON as today; optionally a `citations` table
keyed by `ruling_id` for queryability. Display in the Discord embed as a short
list (`title · locator` + link); full quote on Discord is often too long — link
out or truncate.

**Unknowns / empty retrieval:** if no passage supports a needed fact, the model
must put it in `unknowns` (legal plea) and must not invent a URL. Validation
rejects citations whose `retrieval_id` is missing from the packed set.

## Cost and latency (rough)

Assumptions: Claude Sonnet-class judge, small retrieval pack (~2–4k tokens).

| Piece | Latency | Cost (order of magnitude) |
|---|---|---|
| Wiki fetch (1–3 pages, cached) | 200–800 ms cold; ~0 cached | Bandwidth only if self-hosted fetch |
| Embedding search (if added) | 50–200 ms | Tiny vs generation |
| Judge call (tool use, current) | 3–12 s | Dominant cost (~1 judge call / fight) |
| Judge + retrieval pack | +10–30% tokens | +10–30% $ per fight |
| Challenge | same as fight | Counts against guild daily cap |

Caching wiki pages per fighter name (TTL 24h) keeps repeat matchups cheap.
Guild daily cap (already shipped) remains the primary cost brake.

**Cheaper path:** skip embeddings; keyword / title match into a curated
snippet pack checked into the repo for frequent fighters. **Richer path:**
vector index over wiki dumps — higher ops burden.

## What "I don't know that material" looks like

When retrieval returns nothing useful for a fighter or a contested fact:

- `unknowns` includes a clear plea, e.g. `"No retrieved canon for Kefla's
  stamina under prolonged beam struggle"`.
- `confidence` should drop when the winner hinges on an unknown.
- Embed shows Unknowns prominently (already does).
- Optional: footer `receipts: 0 verified` when all citations failed validation.

## Open questions for Matt

1. **Source allowlist** — which wikis / official texts are in-bounds for v1?
   Any hard nos (e.g. VS Battles Wiki)?
2. **Primary canon** — are you willing to paste exhibits for flagship bouts, or
   should the bot fetch autonomously?
3. **Citation bar** — must every steelman sentence have a receipt, or only the
   load-bearing claims in `ruling`?
4. **Storage** — keep citations only inside verdict JSON, or normalize a
   `citations` table for `/standings` / future search?
5. **Licensing** — OK to store short quotes in `data/court.db`, or links +
   locators only?
6. **UX** — Discord-only receipts, or also mirror to the Notion Court Records
   page?
7. **Budget** — target max $ / guild / day beyond the existing 50-call cap?
8. **Failure mode** — if retrieval is down, judge with legal-plea bias, or
   refuse the fight?

## Non-goals (this design)

- Building a general RAG platform.
- Auto-editing the Notion Court Records page.
- Trusting model-named URLs without a retrieval_id check.
