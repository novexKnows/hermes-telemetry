"""Unit tests for redaction.py — allowlist-only sanitizer (HTB-B1).

These are the defense-layer primitives; the per-capture-point NEGATIVE
redaction tests live in test_envelope_emission.py.
"""

from __future__ import annotations

import hermes_telemetry.redaction as redaction

PROMPT = "PROMPT-LEAK-MARKER the user asked something secret"
FAKE_KEY = "sk-FAKE-TEST-KEY-0123456789abcdef"
FILE_CONTENT = "FILE-CONTENT-LEAK-MARKER contents of /etc/passwd etc."


def test_allowlist_drops_unknown_keys():
    payload = {
        "session_id": "s1",
        "model": "m1",
        "user_message": PROMPT,
        "result": FILE_CONTENT,
        "api_key": FAKE_KEY,
    }
    out = redaction.sanitize(payload)
    assert out == {"session_id": "s1", "model": "m1"}


def test_allowlist_recurses_into_nested_dicts_and_lists():
    # Unknown parent keys are dropped outright...
    payload = {
        "session_id": "s1",
        "tool_name": "read_file",
        "request_messages": [{"role": "user", "content": PROMPT}],
        "args": {"path": "C:/x", "contents": FILE_CONTENT},
        "raw": FAKE_KEY,
    }
    assert redaction.sanitize(payload) == {"session_id": "s1", "tool_name": "read_file"}
    # ...and when a caller-supplied allowlist permits a nested container,
    # filtering still applies INSIDE it.
    nested = redaction.sanitize(
        {"outer": {"inner_ok": 1, "inner_secret": FAKE_KEY}, "dropped": PROMPT},
        allowlist=frozenset({"outer", "inner_ok"}),
    )
    assert nested == {"outer": {"inner_ok": 1}}


def test_scrub_replaces_secret_shapes_in_allowed_values():
    out = redaction.sanitize({"model": f"gpt-x {FAKE_KEY} gpt-y"})
    assert FAKE_KEY not in out["model"]
    assert "[REDACTED]" in out["model"]


def test_scrub_patterns_cover_common_secret_families():
    cases = [
        f"bearer {FAKE_KEY}",
        "Bearer abcdefghijklmnopqrstuvwx",
        "ghp_" + "a" * 30,
        "github_pat_" + "a" * 22,
        "AKIA" + "A1B2C3D4E5F6G7H8I9J0",
        "xoxb-1234-abcdefabcdef",
        "https://abcd1234abcd1234@o123456.ingest.sentry.io/4501",
        "-----BEGIN RSA PRIVATE KEY-----",
        "postgres://admin:hunter2secret@db.internal/prod",
    ]
    for text in cases:
        assert "[REDACTED]" in redaction.sanitize({"model": text})["model"], text


def test_normal_model_and_provider_ids_not_scrubbed():
    benign = {
        "model": "z-ai/glm-5.3-flash",
        "provider": "openrouter",
        "tool_name": "read_file",
        "session_id": "cron_job1_20260919_120000",
        "status": "ok",
        "finish_reason": "stop",
    }
    assert redaction.sanitize(benign) == benign


def test_sanitize_never_raises_and_drops_unrepresentable():
    weird = {"session_id": "s1", "blob": object(), "result": b"bytes-payload"}
    out = redaction.sanitize(weird)
    assert out == {"session_id": "s1"}


def test_sanitize_empty_and_none():
    assert redaction.sanitize(None) is None
    assert redaction.sanitize({}) == {}


def test_custom_allowlist_narrows_further():
    out = redaction.sanitize(
        {"session_id": "s1", "model": "m1"}, allowlist=frozenset({"session_id"})
    )
    assert out == {"session_id": "s1"}


def test_scrub_hits_counter_counts_and_resets():
    redaction.reset_stats()
    redaction.sanitize({"model": f"x {FAKE_KEY} y {FAKE_KEY}"})
    assert redaction.scrub_hits() >= 2
    redaction.reset_stats()
    assert redaction.scrub_hits() == 0
