---
name: social-learning-setup
description: "Install and configure the social-learning Hermes plugin end-to-end: clone, enable, set service URL + API key, optional group voice, verify."
version: 1.0.0
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [social-learning, plugin, setup, install, configure, voice-card, humalike]
    related_skills: []
---

# Configure the social-learning plugin

You are setting up the **social-learning** Hermes plugin. It calls an external
HTTP service that turns a conversation transcript into a "voice card" and
injects it into the prompt so the agent mirrors how the conversation talks.

Work through the steps in order. **Ask the user for the two deployment-specific
values** (service URL and API key) — never invent them. **Never print the API
key** back to the user or into logs.

## Step 0 — Prerequisites

Confirm Hermes is installed and you can run it:

```bash
hermes --version
```

If `hermes` is not on PATH but you are in the Hermes repo, use the venv launcher
(`.venv/bin/hermes`). All commands below assume `hermes` resolves correctly.

Resolve the Hermes home dir (config + .env live here):

```bash
python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"   # usually ~/.hermes
```

## Step 1 — Install the plugin (drop-in)

Clone this repo into the user-plugins directory so the plugin folder is
`<hermes_home>/plugins/social-learning/` (containing `plugin.yaml`):

```bash
git clone https://github.com/maksbilski/hermes-social-learning ~/.hermes/plugins/social-learning
```

If it already exists, `git -C ~/.hermes/plugins/social-learning pull` instead.

Verify it is discovered:

```bash
hermes plugins list | grep social-learning
```

## Step 2 — Enable it (plugins are opt-in)

```bash
hermes plugins enable social-learning
```

## Step 3 — Set the service URL

Ask the user for the base URL of their voice-card service (the plugin POSTs to
`{service_url}/v1/social-learning/extract`). Then:

```bash
hermes config set social_learning.service_url "<SERVICE_URL>"
```

Verify it round-trips:

```bash
grep -A1 '^social_learning:' ~/.hermes/config.yaml
```

## Step 4 — Set the API key (do NOT echo it)

Ask the user for the API key. Store it in `<hermes_home>/.env` as
`SOCIAL_LEARNING_API_KEY` (sent as the `X-API-Key` header). Append without
printing the value:

```bash
# remove any prior line, then append; KEY is provided by the user
grep -v '^SOCIAL_LEARNING_API_KEY=' ~/.hermes/.env > ~/.hermes/.env.tmp 2>/dev/null || true
mv ~/.hermes/.env.tmp ~/.hermes/.env 2>/dev/null || true
printf 'SOCIAL_LEARNING_API_KEY=%s\n' "<API_KEY>" >> ~/.hermes/.env
chmod 600 ~/.hermes/.env
echo "key written: $(grep -c '^SOCIAL_LEARNING_API_KEY=' ~/.hermes/.env) line(s)"
```

Optionally sanity-check the endpoint (do not print the key):

```bash
python - <<'PY'
import os, json, urllib.request, urllib.error, pathlib
from hermes_cli.env_loader import load_hermes_dotenv
load_hermes_dotenv(project_env=pathlib.Path('.env'))
from hermes_cli.config import load_config, cfg_get
base = (cfg_get(load_config(), "social_learning", "service_url", default="") or "").rstrip("/")
key = os.environ.get("SOCIAL_LEARNING_API_KEY", "")
body = json.dumps({"transcript": {"messages": [{"id": "0", "author": "user", "text": "hi"}]}}).encode()
req = urllib.request.Request(base + "/v1/social-learning/extract", data=body,
                            headers={"X-API-Key": key, "Content-Type": "application/json"})
try:
    r = urllib.request.urlopen(req, timeout=60); d = json.load(r)
    print("OK", r.status, "prompt_block chars:", len(d.get("prompt_block") or ""))
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read().decode()[:200])
PY
```

## Step 5 — Group voice (required for group chats)

**Required** whenever the agent runs in group chats. The plugin keys by
`session_id`, and Hermes defaults to `group_sessions_per_user: true` (one session
per participant), which produces a voice card per person instead of per group.
To get **one voice card per group**, set the top-level key:

```bash
hermes config set group_sessions_per_user false
```

Tell the user this makes Hermes treat the group as one shared conversation
(shared context across participants). DMs are unaffected — skip only for
DM-only deployments.

## Step 6 — (Optional) Debug logging

Off by default. Enable to dump exact request payloads to JSONL:

```bash
hermes config set social_learning.log_requests true
```

When on, requests are written to `<hermes_home>/logs/social-learning-requests.jsonl`.

## Step 7 — Apply

Plugins load at process start. Restart the gateway (or start a fresh CLI
session) so the plugin and config take effect:

```
hermes gateway run        # or restart however the user runs Hermes
```

## Step 8 — Verify

After 5+ messages in one conversation, the slow clock fires and the card is
cached; subsequent replies inject it. Watch the logs:

```bash
tail -n 50 ~/.hermes/logs/agent.log | grep social-learning
```

Expected sequence: `firing detached refresh` → `POST {service_url}/v1/social-learning/extract`
→ `refreshed voice card (... chars ...)` → `injecting voice card ...`.

If you see `non-200 response` repeatedly, check the API key and service URL. If
nothing appears at all, confirm the plugin is enabled (`hermes plugins list`)
and that the gateway was restarted after enabling.

## Notes / scope

- This skill configures the plugin only. It does **not** manage who may message
  the bot (access control), platform pairing, or the model/provider.
- The plugin is in-memory only (cards reset on restart), has no retry/backoff,
  and treats each transcript line as a message (see the plugin README for the
  full v0 limitations).
