"""Tests for the social-learning plugin (../__init__.py).

The plugin dir name contains a hyphen, so it is not importable as a normal
package. It is loaded once at module level via importlib.
"""

import importlib.util
import pathlib

import pytest

# ── Module load ───────────────────────────────────────────────────────────────

_P = pathlib.Path(__file__).resolve().parent.parent / "__init__.py"
spec = importlib.util.spec_from_file_location("social_learning_plugin", _P)
sl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sl)


# ── Isolation fixture ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    """Clear module-level dicts before each test."""
    sl._CACHE.clear()
    sl._COUNTER.clear()
    yield
    sl._CACHE.clear()
    sl._COUNTER.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fake_response(status_code=200, json_body=None):
    """Return a minimal fake requests.Response-like object."""

    class _FakeResp:
        def __init__(self, code, body):
            self.status_code = code
            self._body = body or {}

        def json(self):
            return self._body

    return _FakeResp(status_code, json_body)


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_register_wires_hook():
    """register(ctx) must call ctx.register_hook('pre_llm_call', on_pre_llm_call)."""

    class _FakeCtx:
        def __init__(self):
            self.calls = []

        def register_hook(self, name, cb):
            self.calls.append((name, cb))

    ctx = _FakeCtx()
    sl.register(ctx)

    assert ("pre_llm_call", sl.on_pre_llm_call) in ctx.calls


def test_build_transcript_filters_and_windows():
    """_build_transcript keeps only user-role msgs with non-empty str content."""
    history = [
        {"role": "user", "content": "hello"},          # valid
        {"role": "assistant", "content": "hi there"},   # excluded: assistant
        {"role": "user", "content": ""},                # excluded: empty string
        {"role": "user", "content": None},              # excluded: non-string
        {"role": "user", "content": 42},                # excluded: non-string
        {"role": "user", "content": "second valid"},    # valid
    ]

    result = sl._build_transcript(history)

    assert len(result) == 2
    assert result[0] == {"id": "0", "author": "user", "text": "hello"}
    assert result[1] == {"id": "1", "author": "user", "text": "second valid"}


def test_build_transcript_windows_to_last_window_entries():
    """With >WINDOW user messages, only the last WINDOW are returned, ids restart at 0."""
    # Build WINDOW + 5 user messages
    total = sl.WINDOW + 5
    history = [{"role": "user", "content": f"msg-{i}"} for i in range(total)]

    result = sl._build_transcript(history)

    assert len(result) == sl.WINDOW
    # First item should be the (total - WINDOW)-th message
    first_expected_text = f"msg-{total - sl.WINDOW}"
    assert result[0]["text"] == first_expected_text
    # ids restart at "0"
    assert result[0]["id"] == "0"
    assert result[-1]["id"] == str(sl.WINDOW - 1)


def test_build_transcript_lifts_sender_prefix_into_author():
    """A "[Name] text" group message → author=Name, text without the prefix."""
    history = [{"role": "user", "content": "[Mateusz] siema"}]
    result = sl._build_transcript(history)
    assert result == [{"id": "0", "author": "Mateusz", "text": "siema"}]


def test_build_transcript_unglues_multiline_and_carries_author():
    """A batched multi-line turn → one message per line, author carried forward."""
    history = [{"role": "user", "content": "[Mateusz] siema\nco tam u was\nsmakowały kanapki?"}]
    result = sl._build_transcript(history)
    assert [(m["author"], m["text"]) for m in result] == [
        ("Mateusz", "siema"),
        ("Mateusz", "co tam u was"),
        ("Mateusz", "smakowały kanapki?"),
    ]
    assert [m["id"] for m in result] == ["0", "1", "2"]


def test_build_transcript_multiple_speakers_get_distinct_authors():
    """Different [Name] prefixes across turns yield distinct authors."""
    history = [
        {"role": "user", "content": "[Mateusz] elo"},
        {"role": "assistant", "content": "hej"},
        {"role": "user", "content": "[Mateusz Winiarek] no siema"},
    ]
    result = sl._build_transcript(history)
    assert [(m["author"], m["text"]) for m in result] == [
        ("Mateusz", "elo"),
        ("Mateusz Winiarek", "no siema"),
    ]


def test_build_transcript_drops_control_markers():
    """Hermes control markers / media notes are stripped, not treated as authors."""
    content = (
        "[Observed Telegram group context - context only, not requests]\n"
        "[Mateusz] stara wiadomosc\n"
        "[New message]\n"
        "[Mateusz Winiarek] nowa\n"
        "[User sent an image: file:///tmp/x.jpg]"
    )
    history = [{"role": "user", "content": content}]
    result = sl._build_transcript(history)
    assert [(m["author"], m["text"]) for m in result] == [
        ("Mateusz", "stara wiadomosc"),
        ("Mateusz Winiarek", "nowa"),
    ]


def test_build_transcript_dm_without_prefix_defaults_to_user():
    """DM content has no [Name] prefix → author stays 'user'."""
    history = [{"role": "user", "content": "hej co tam"}]
    result = sl._build_transcript(history)
    assert result == [{"id": "0", "author": "user", "text": "hej co tam"}]


def test_refresh_card_caches_prompt_block_on_200(monkeypatch):
    """On HTTP 200 the 'prompt_block' from the response JSON is stored in _CACHE."""
    monkeypatch.setattr(sl, "_get_service_url", lambda: "https://svc.test")
    monkeypatch.setenv(sl.API_KEY_ENV, "test-key-abc")

    fake_resp = _fake_response(200, {"profile": {"x": 1}, "prompt_block": "VOICE_CARD"})
    post_calls = []

    def _fake_post(url, headers=None, json=None, timeout=None):
        post_calls.append({"url": url, "headers": headers, "json": json})
        return fake_resp

    monkeypatch.setattr(sl.requests, "post", _fake_post)

    sl._refresh_card("sid1", [{"role": "user", "content": "hi"}])

    assert sl._CACHE["sid1"] == "VOICE_CARD"

    assert len(post_calls) == 1
    call = post_calls[0]
    assert call["url"] == "https://svc.test" + sl.SERVICE_PATH
    assert "X-API-Key" in call["headers"]
    assert "Content-Type" in call["headers"]
    assert "transcript" in call["json"]
    assert "messages" in call["json"]["transcript"]
    assert len(call["json"]["transcript"]["messages"]) >= 1


def test_refresh_card_skips_on_502(monkeypatch):
    """On non-200 response _CACHE stays empty and no exception is raised."""
    monkeypatch.setattr(sl, "_get_service_url", lambda: "https://svc.test")
    monkeypatch.setenv(sl.API_KEY_ENV, "test-key-abc")

    fake_resp = _fake_response(502)
    monkeypatch.setattr(sl.requests, "post", lambda *a, **kw: fake_resp)

    sl._refresh_card("sid2", [{"role": "user", "content": "hi"}])

    assert "sid2" not in sl._CACHE


def test_refresh_card_no_messages_skips(monkeypatch):
    """When the filtered transcript is empty, requests.post is never called."""
    monkeypatch.setattr(sl, "_get_service_url", lambda: "https://svc.test")
    monkeypatch.setenv(sl.API_KEY_ENV, "test-key-abc")

    post_called = []
    monkeypatch.setattr(sl.requests, "post", lambda *a, **kw: post_called.append(1))

    # Only assistant messages — nothing passes the filter
    sl._refresh_card("sid3", [{"role": "assistant", "content": "hi"}])

    assert post_called == []
    assert "sid3" not in sl._CACHE


def test_refresh_card_never_raises_on_network_error(monkeypatch):
    """requests.post raising RequestException must not propagate out of _refresh_card."""
    import requests as _req

    monkeypatch.setattr(sl, "_get_service_url", lambda: "https://svc.test")
    monkeypatch.setenv(sl.API_KEY_ENV, "test-key-abc")

    def _boom(*a, **kw):
        raise _req.RequestException("connection refused")

    monkeypatch.setattr(sl.requests, "post", _boom)

    # Must not raise
    sl._refresh_card("sid4", [{"role": "user", "content": "hi"}])

    assert "sid4" not in sl._CACHE


def test_hook_returns_none_until_card_present_then_injects(monkeypatch):
    """on_pre_llm_call returns None when no card is cached, dict once a card is set."""
    # Empty base URL so no thread is spawned (transcript POST gate is never reached)
    monkeypatch.setattr(sl, "_get_service_url", lambda: "")

    history = [{"role": "user", "content": "test"}]

    result_before = sl.on_pre_llm_call(
        session_id="sidX", conversation_history=history
    )
    assert result_before is None
    assert "sidX" not in sl._CACHE

    sl._CACHE["sidX"] = "CARD"

    result_after = sl.on_pre_llm_call(
        session_id="sidX", conversation_history=history
    )
    assert result_after == {"context": "CARD"}


def test_hook_fires_refresh_every_fifth_turn(monkeypatch):
    """_refresh_card is invoked on turns 5 and 10 for the same session_id, not on others."""
    refresh_calls = []

    def _fake_refresh(session_id, conversation_history):
        refresh_calls.append(session_id)

    # Replace threading.Thread so the body runs synchronously and we can
    # assert deterministically.
    class _SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target = target
            self._args = args
            self._kwargs = kwargs or {}

        def start(self):
            if self._target:
                self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(sl.threading, "Thread", _SyncThread)
    monkeypatch.setattr(sl, "_refresh_card", _fake_refresh)
    # Must return truthy so on_pre_llm_call's guard `and _get_service_url()`
    # passes and actually spawns the thread.
    monkeypatch.setattr(sl, "_get_service_url", lambda: "https://svc.test")

    history = [{"role": "user", "content": "msg"}]
    session = "session-refresh-test"

    for turn in range(1, 11):
        sl.on_pre_llm_call(session_id=session, conversation_history=history)

        if turn in (5, 10):
            assert refresh_calls.count(session) == turn // sl.REFRESH_EVERY, (
                f"Expected _refresh_card called {turn // sl.REFRESH_EVERY} time(s) after turn {turn}"
            )
        else:
            expected = (turn // sl.REFRESH_EVERY)
            assert refresh_calls.count(session) == expected, (
                f"Unexpected _refresh_card call count after turn {turn}"
            )


def test_hook_returns_none_without_session_id():
    """on_pre_llm_call with no session_id keyword returns None."""
    result = sl.on_pre_llm_call(conversation_history=[{"role": "user", "content": "hi"}])
    assert result is None
