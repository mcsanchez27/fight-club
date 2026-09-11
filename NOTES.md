# NOTES — ideas not implemented in this round

Per brief: do not change working code unless a listed item requires it.
Extra ideas live here only.

1. **OpenAI tool-use parity** — Anthropic uses `deliver_verdict` tool use;
   OpenAI fallback still uses `response_format=json_object`. Could add OpenAI
   tools later for symmetry; not required by the brief.

2. **Persistent Challenge views after restart** — message-id memory is now
   backed by SQLite lookup in `get_ruling`, so Challenge on an old message
   works after restart if the row exists. Button custom_id was already
   persistent.

3. **Cooldown refund on judge failure** — we `record()` before the LLM call so
   failed judgments still consume cooldown/cap. Safer for cost; slightly
   harsher UX. Could record only on success later.

4. **UTC vs America/Denver for daily cap** — cap day key uses `date.today()`
   in the host local/UTC environment. Document or pin to UTC explicitly if
   Matt cares about timezone boundaries.

5. **Embed still puts ruling in description** — steelman-first schema order is
   enforced for the model; Discord embed keeps ruling as description for
   readability, with steelmans as fields above winner. Could invert if desired.

6. **`/docket remove` / claim / run** — only add/list were requested.

7. **pytest in requirements.txt** — added so CI/local test install is one file;
   runtime bot does not need it.

8. **Remove OpenAI dependency** — brief says lean Anthropic stack; OpenAI
   remains as fallback from prior commits. Not removed.
