# social-learning (Hermes plugin)

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that makes
the agent match **how each conversation talks** by integrating with the
**Social Learning API**, powered by [Humalike](https://humalike.com).

The **Social Learning API** turns a chat transcript into a "voice card" — a
prompt block describing a conversation's style: tone, formatting, lexicon,
in-jokes, and norms. See the [docs](https://docs.humalike.com).

This plugin is the Hermes integration for that API. It watches the conversation,
sends the recent transcript to the Social Learning API, caches the returned voice
card, and injects it into the prompt before each reply so the model mirrors the
conversation's tone. It does no analysis itself — the API produces the card; the
plugin only observes, calls, caches, and injects.

## Install (drop-in)

```bash
git clone https://github.com/maksbilski/hermes-social-learning ~/.hermes/plugins/social-learning
hermes plugins enable social-learning
```

Point the plugin at the Social Learning API in `~/.hermes/config.yaml`:

```yaml
social_learning:
  service_url: "https://api.humalike.com"   # Social Learning API; POSTs to {service_url}/v1/social-learning/extract
  log_requests: false                        # optional debug: dump request payloads to JSONL
```

Set your Social Learning API key (sent as the `X-API-Key` header) — get one from
your [Humalike dashboard](https://humalike.com):

```bash
export SOCIAL_LEARNING_API_KEY="your-humalike-api-key"   # or add to ~/.hermes/.env
```

Finally restart the gateway (or start a fresh session) so the plugin loads.

### Configure with an agent

Prefer to let an agent do it? This repo ships [`SKILL.md`](SKILL.md) — a Hermes
skill that walks an agent through the full setup (clone, enable, set service URL
+ API key, optional group voice, restart, verify). Point your agent at it, e.g.
"follow SKILL.md to set up social-learning", and answer its prompts for the
service URL and API key.

## Group chats — required setting

To get **one voice card per group** (instead of one per participant), set this
top-level key in `~/.hermes/config.yaml`:

```yaml
group_sessions_per_user: false
```

Without it, Hermes gives each participant a separate session, so the plugin
learns a card per person. DMs are unaffected.

## Social Learning API contract

The plugin calls the [Social Learning API](https://docs.humalike.com) (powered by
Humalike) once per refresh:

`POST {service_url}/v1/social-learning/extract`

Headers: `X-API-Key`, `Content-Type: application/json`

Request:
```json
{
  "transcript": {
    "messages": [
      {"id": "0", "author": "Mateusz", "text": "siema"},
      {"id": "1", "author": "Mateusz Winiarek", "text": "no elo"}
    ]
  }
}
```

Response (200):
```json
{ "profile": { "...": "..." }, "prompt_block": "Match this community's voice. Write..." }
```

The plugin uses only `prompt_block`. 400 / 502 / timeouts / other non-200 are
skipped silently and retried on the next 5-turn cycle. Full schema:
[docs.humalike.com](https://docs.humalike.com/api-reference/extract).

## Limitations (v0)

- **No persistence** — cards live in memory; lost on gateway restart.
- **No throttling / backoff** — bursts may hit service rate limits; the card
  just refreshes on the next cycle.
- **No concurrency dedupe** — overlapping refreshes possible if the service is slow.
- **No refusal handling** — non-200 responses are skipped, not inspected.
- **No conversation filtering** — every session is eligible.
- **Hardcoded cadence/window** — refresh-every-5 and last-100 are constants.
- **`author` is the display name**, not a stable platform id — the same person
  under two push-names can appear as two authors.
- **Heuristic message boundaries** — Hermes concatenates batched messages with
  newlines before the plugin sees them, so genuinely multi-line single messages
  get split into lines.
- **Hermes-specific injection point** — the card is appended to the user message
  (not the system prompt) because Hermes injects plugin context there to
  preserve the prompt cache.

## Testing

```bash
python -m pytest tests/ -q
```

## License

MIT — see [LICENSE](LICENSE).
