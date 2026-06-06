# social-learning (Hermes plugin)

Makes a [Hermes Agent](https://github.com/NousResearch/hermes-agent) match **how
each conversation talks**. It calls an external HTTP service that turns the
recent transcript into a "voice card" (a prompt block describing the
conversational style), and injects that card into the prompt before each reply
so the model mirrors the conversation's tone.

> The voice-card extraction lives in your own service (e.g. humalike). This
> plugin is the Hermes integration: it observes the conversation, calls the
> service, caches the card, and injects it.

## How it works — two clocks in one hook

Both loops live in the `pre_llm_call` hook, keyed by `session_id`:

| Clock | When | What |
|---|---|---|
| **Slow** | Every 5th turn | Fire a detached (non-blocking) POST to the service with the last 100 messages; cache the returned `prompt_block`. Never delays the reply. |
| **Fast** | Every reply | Read the cached card for this session (one in-memory lookup) and append it to the prompt. No card yet → nothing injected. |

State is **in-memory only**, keyed by `session_id`. Restarting the gateway
clears all cards.

## Install (drop-in)

```bash
git clone <this-repo-url> ~/.hermes/plugins/social-learning
hermes plugins enable social-learning
```

Configure the service in `~/.hermes/config.yaml`:

```yaml
social_learning:
  service_url: "https://api.example.com"   # POSTs to {service_url}/v1/social-learning/extract
  log_requests: false                       # optional debug: dump request payloads + outgoing prompts
```

Set the API key (sent as `X-API-Key`):

```bash
export SOCIAL_LEARNING_API_KEY="your-api-key"   # or add to ~/.hermes/.env
```

Finally restart the gateway (or start a fresh session) so the plugin loads.

### Configure with an agent

Prefer to let an agent do it? This repo ships [`SKILL.md`](SKILL.md) — a Hermes
skill that walks an agent through the full setup (clone, enable, set service URL
+ API key, optional group voice, restart, verify). Point your agent at it, e.g.
"follow SKILL.md to set up social-learning", and answer its prompts for the
service URL and API key.

### Verify it works

After 5+ messages in one conversation:

```bash
tail -n 50 ~/.hermes/logs/agent.log | grep social-learning
```

Expected: `firing detached refresh` → `POST .../v1/social-learning/extract` →
`refreshed voice card (… chars …)` → `injecting voice card …`. Repeated
`non-200 response` → check the API key / service URL. Nothing at all → confirm
`hermes plugins list` shows it enabled and that you restarted the gateway.

## Group chats — required setting

The plugin keys by **session_id**. In groups, Hermes defaults to
`group_sessions_per_user: true`, giving each participant a separate session — so
the plugin would learn a separate card per person and nobody reaches the 5-turn
refresh. To learn **one shared voice for the whole group**, set this top-level
key in `~/.hermes/config.yaml`:

```yaml
group_sessions_per_user: false
```

This makes Hermes treat the group as one shared conversation (shared context
across participants) — usually what you want for a community-voice bot. DMs are
unaffected.

## External service contract

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
{ "profile": "...", "prompt_block": "Match this community's voice. Write..." }
```

The plugin uses only `prompt_block`. 400 / 502 / timeouts / other non-200 are
skipped silently and retried on the next 5-turn cycle.

## Transcript handling

Hermes formats the user text before the plugin sees it (group sender prefix
`[Name] `, channel-context backfill, control markers). The plugin:

- lifts the `[Name]` prefix into the message `author`,
- drops Hermes control markers (`[New message]`, `[User sent …]`, etc.),
- splits each turn's lines into separate messages (people send short, separate
  lines, so a newline is almost always a message boundary).

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

`examples/mock_sl.py` is a tiny local stand-in for the service for end-to-end
testing without the real backend.

## License

MIT — see [LICENSE](LICENSE).
