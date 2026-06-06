"""social-learning plugin — matches the agent's reply style to how each conversation talks.

Design: two clocks in one hook
================================
All work happens inside ``on_pre_llm_call``, the single registered hook.

SLOW CLOCK (refresh path)
    Every REFRESH_EVERY turns a daemon thread is spawned to POST the recent
    transcript to an external HTTP service.  The service replies with a
    ``prompt_block`` — a short "voice card" describing the speaker's style.
    The thread writes the card into _CACHE under _LOCK and exits silently.
    Failures are discarded; the previous card remains in cache.

FAST CLOCK (inject path)
    On every call, the hook reads _CACHE under _LOCK and returns
    ``{"context": card}`` so Hermes injects the voice card into the prompt
    before the LLM is called.  If no card exists yet, it returns None and
    the LLM call proceeds unchanged.

Config (config.yaml)
--------------------
    social_learning:
      service_url: "https://api.example.com"   # required; POSTs to {service_url}/v1/social-learning/extract
      log_requests: false                       # optional; when true, dump request payloads + outgoing prompts (debug)
Env: SOCIAL_LEARNING_API_KEY  (sent as the X-API-Key header)

v0 omissions (intentional, not bugs)
--------------------------------------
- No persistence across process restarts (in-memory only).
- No throttling / exponential back-off on service errors.
- No concurrency deduplication (last writer wins; acceptable for a style hint).
- No refusal handling (non-200 responses are skipped, retried next cycle).
- No conversation filtering (all sessions are eligible).
- Transcript boundaries are heuristic: Hermes concatenates batched messages
  with newlines before the plugin sees them, so each line is treated as a
  separate message (author lifted from the "[Name]" group prefix).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REFRESH_EVERY: int = 5
WINDOW: int = 100
SERVICE_PATH: str = "/v1/social-learning/extract"
API_KEY_ENV: str = "SOCIAL_LEARNING_API_KEY"

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_LOCK: threading.Lock = threading.Lock()
_CACHE: Dict[str, str] = {}   # session_id -> prompt_block (voice card)
_COUNTER: Dict[str, int] = {}  # session_id -> turn count


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_service_url() -> str:
    """Return the configured service base URL, or '' if not set / on error."""
    try:
        from hermes_cli.config import load_config, cfg_get  # noqa: PLC0415
        url = cfg_get(load_config(), "social_learning", "service_url", default="") or ""
        return url.rstrip("/")
    except Exception:
        return ""


def _log_requests_enabled() -> bool:
    """Whether to write debug artifacts (request JSONL + outgoing prompt logs).

    Off by default; enable with ``social_learning.log_requests: true``.
    """
    try:
        from hermes_cli.config import load_config, cfg_get  # noqa: PLC0415
        return bool(cfg_get(load_config(), "social_learning", "log_requests", default=False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

# Hermes injects control markers and a "[Sender] " prefix into the user
# message text before we ever see it.  We strip the markers and lift the
# "[Name]" prefix into the transcript's `author` field so the service sees
# per-speaker messages instead of one blob authored by "user".
_AUTHOR_RE = re.compile(r"^\[([^\]]{1,60})\]\s*(.*)$")
_CONTROL_PREFIXES = (
    "[New message]",
    "[Observed Telegram group context",
    "[Current addressed message",
    "[User sent ",
    "[The user sent ",
    "[Delivered from ",
    "[IMPORTANT:",
)


def _is_control_marker(line: str) -> bool:
    """True for Hermes-injected control lines that aren't real chat content."""
    return any(line.startswith(p) for p in _CONTROL_PREFIXES)


def _parse_messages(content: str) -> List[Dict[str, str]]:
    """Split one Hermes user-turn into per-speaker, per-line transcript messages.

    Hermes may concatenate several raw chat messages into a single user turn
    (debounce batching + channel-context backfill) and prefixes group messages
    with "[Sender] ".  We split on newlines, drop control markers, and carry the
    most recent "[Sender]" forward as the author so each line becomes an
    attributed message.  DMs have no prefix → author stays "user".
    """
    out: List[Dict[str, str]] = []
    author = "user"
    for raw in content.split("\n"):
        line = raw.strip()
        if not line or _is_control_marker(line):
            continue
        m = _AUTHOR_RE.match(line)
        if m:
            author = m.group(1).strip() or "user"
            text = m.group(2).strip()
        else:
            text = line
        if text:
            out.append({"author": author, "text": text})
    return out


def _build_transcript(conversation_history: Any) -> List[Dict[str, str]]:
    """Filter + parse conversation_history into the service message format.

    Keeps user-role messages, parses each into per-speaker lines (author lifted
    from the "[Name]" prefix, control markers dropped), then returns the last
    WINDOW messages in wire format.
    """
    if not conversation_history:
        return []

    messages: List[Dict[str, str]] = []
    for msg in conversation_history:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "user"
            and isinstance(msg.get("content"), str)
            and msg["content"]
        ):
            messages.extend(_parse_messages(msg["content"]))

    windowed = messages[-WINDOW:]
    return [
        {"id": str(idx), "author": m["author"], "text": m["text"]}
        for idx, m in enumerate(windowed)
    ]


# ---------------------------------------------------------------------------
# Slow-clock worker (detached daemon thread)
# ---------------------------------------------------------------------------

def _log_request(session_id: str, url: str, body: Dict[str, Any]) -> None:
    """Append the exact outgoing request payload to a JSONL file (debug only).

    No-op unless ``social_learning.log_requests`` is true.  When enabled, writes
    one JSON object per refresh to ``<hermes_home>/logs/social-learning-requests.jsonl``
    so the precise shape sent to the service can be verified.  Never raises.
    """
    if not _log_requests_enabled():
        return
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415
        from datetime import datetime  # noqa: PLC0415
        path = get_hermes_home() / "logs" / "social-learning-requests.jsonl"
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_id": session_id,
            "url": url,
            "message_count": len(body.get("transcript", {}).get("messages", [])),
            "body": body,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.debug("social-learning: request log failed: %s", exc)


def _refresh_card(session_id: str, conversation_history: Any) -> None:
    """Fetch a fresh voice card from the external service and cache it.

    Runs in a detached daemon thread.  All exceptions are caught so a failure
    can never propagate back to the agent loop.
    """
    try:
        transcript = _build_transcript(conversation_history)
        if not transcript:
            # Service requires at least one message.
            return

        api_key = os.environ.get(API_KEY_ENV, "")
        base = _get_service_url()
        if not base:
            return

        url = base + SERVICE_PATH
        body = {"transcript": {"messages": transcript}}
        logger.info(
            "social-learning: POST %s (msgs=%d, api_key=%s) for session %s",
            url, len(transcript),
            "set" if api_key else "MISSING", session_id,
        )
        _log_request(session_id, url, body)  # debug-only payload dump
        response = requests.post(
            url,
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json=body,
            timeout=60,
        )

        if response.status_code == 200:
            try:
                data = response.json()
            except Exception as exc:
                logger.debug(
                    "social-learning: failed to parse JSON response for session %s: %s",
                    session_id, exc,
                )
                return
            prompt_block = data.get("prompt_block")
            if isinstance(prompt_block, str) and prompt_block:
                with _LOCK:
                    _CACHE[session_id] = prompt_block
                logger.info(
                    "social-learning: refreshed voice card for session %s "
                    "(%d chars from %d transcript msgs)",
                    session_id, len(prompt_block), len(transcript),
                )
        else:
            logger.info(
                "social-learning: non-200 response %d for session %s (skipping, retry next cycle)",
                response.status_code, session_id,
            )
    except Exception as exc:
        logger.warning(
            "social-learning: _refresh_card failed for session %s: %s",
            session_id, exc,
        )


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def on_pre_llm_call(**kwargs: Any) -> Optional[Dict[str, Any]]:
    """pre_llm_call hook: inject voice card and (every REFRESH_EVERY turns) refresh it.

    Hermes passes: session_id, task_id, turn_id, user_message,
    conversation_history, is_first_turn, model, platform, sender_id,
    telemetry_schema_version.
    """
    try:
        session_id: str = kwargs.get("session_id") or ""
        if not session_id:
            return None

        conversation_history = kwargs.get("conversation_history") or []

        # --- SLOW CLOCK: increment turn counter and maybe spawn a refresh ---
        with _LOCK:
            _COUNTER[session_id] = _COUNTER.get(session_id, 0) + 1
            n = _COUNTER[session_id]
            has_card = session_id in _CACHE

        # Per-turn heartbeat so liveness is visible from the very first message.
        logger.debug(
            "social-learning: turn %d session=%s card=%s (refresh every %d turns)",
            n, session_id, "cached" if has_card else "none", REFRESH_EVERY,
        )

        if n % REFRESH_EVERY == 0 and _get_service_url():
            logger.info(
                "social-learning: turn %d for session %s — firing detached refresh",
                n, session_id,
            )
            threading.Thread(
                target=_refresh_card,
                args=(session_id, list(conversation_history)),
                daemon=True,
            ).start()

        # --- FAST CLOCK: return cached card (if any) ---
        with _LOCK:
            card = _CACHE.get(session_id)

        if card:
            logger.info(
                "social-learning: injecting voice card for session %s (%d chars)",
                session_id, len(card),
            )
            return {"context": card}
        return None

    except Exception as exc:
        logger.warning("social-learning: on_pre_llm_call failed: %s", exc)
        return None


def log_outgoing_user_prompt(**kwargs: Any) -> None:
    """Debug hook: log the full user message actually sent to the model this turn.

    No-op unless ``social_learning.log_requests`` is true.  ``pre_api_request``
    carries ``request_messages`` = the exact message list sent to the LLM, AFTER
    core appends our pre_llm_call context to the user turn — so the last user
    message is the real prompt (user text + injected voice card).  Observer only.
    """
    if not _log_requests_enabled():
        return None
    try:
        session_id: str = kwargs.get("session_id") or ""
        messages = kwargs.get("request_messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                text = content if isinstance(content, str) else str(content)
                logger.info(
                    "social-learning: OUTGOING user prompt (session=%s, %d chars):\n%s",
                    session_id, len(text), text,
                )
                break
    except Exception as exc:
        logger.warning("social-learning: outgoing-prompt log failed: %s", exc)
    return None


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("pre_api_request", log_outgoing_user_prompt)
