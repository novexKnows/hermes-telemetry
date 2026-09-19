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


# ---------------------------------------------------------------------------
# Unicode default-ignorable obfuscation (reviewer-adversarial class):
# a U+200B inserted after "sk-" previously defeated shape matching and let
# the exact secret value ride on allowlisted fields.
# ---------------------------------------------------------------------------

_SECRET_BODY = "FAKEKEY0123456789abcdef"

# One representative per default-ignorable class: Cf zero-width / bidi
# controls, Cf variation selectors, Zl / Zp separators, and the known
# default-ignorable outliers (Mn / Lo / Cn).
DEFAULT_IGNORABLE_SEPARATORS = {
    "zero-width-space-U+200B": "\u200b",
    "zero-width-non-joiner-U+200C": "\u200c",
    "zero-width-joiner-U+200D": "\u200d",
    "rtl-mark-U+200F": "\u200f",
    "word-joiner-U+2060": "\u2060",
    "bidi-isolate-init-U+2066": "\u2066",
    "soft-hyphen-U+00AD": "\u00ad",
    "bom-U+FEFF": "\ufeff",
    "variation-selector-16-U+FE0F": "\ufe0f",
    "variation-selector-1-U+FE00": "\ufe00",
    "line-separator-U+2028": "\u2028",
    "paragraph-separator-U+2029": "\u2029",
    "bidi-embed-U+202B": "\u202b",
    "combining-grapheme-joiner-U+034F": "\u034f",
    "khmer-inherent-vowel-U+17B4": "\u17b4",
    "mongolian-fvs-1-U+180B": "\u180b",
    "hangul-filler-U+3164": "\u3164",
    "tag-char-U+E0001": "\U000e0001",
}


def test_default_ignorable_obfuscated_sk_secrets_still_scrubbed():
    for label, sep in DEFAULT_IGNORABLE_SEPARATORS.items():
        out = redaction.sanitize({"model": f"sk-{sep}{_SECRET_BODY}"})["model"]
        assert "[REDACTED]" in out, f"secret leaked through separator {label}: {out!r}"
        assert _SECRET_BODY not in out, f"secret body leaked via {label}"
        assert sep not in out, f"default-ignorable {label} survived emission"


def test_default_ignorable_interleaved_inside_secret_body_still_scrubbed():
    # Separators interleaved mid-body, not just after the prefix.
    obfuscated = "sk-FAKE\u200bKEY012345\u20606789abcdef\u00ad"
    out = redaction.sanitize({"model": obfuscated})["model"]
    assert "[REDACTED]" in out and _SECRET_BODY not in out


def test_default_ignorable_obfuscated_other_secret_families_scrubbed():
    cases = {
        "ghp": f"ghp_\u2060{'a' * 30}",
        "bearer": f"Bearer\u200b {'a' * 24}",
        "aws": "AKIA\u200bA1B2C3D4E5F6G7H8I9J0",
    }
    for family, text in cases.items():
        out = redaction.sanitize({"model": text})["model"]
        assert "[REDACTED]" in out, f"{family} leaked through default-ignorable: {out!r}"


def test_default_ignorables_stripped_from_benign_values_emitted_normalized():
    # The emitted value is the NORMALIZED (stripped) form — nothing ignorable
    # can survive on any emitted string, secret-shaped or not.
    assert redaction.sanitize({"model": "glm\u200b-5.3"})["model"] == "glm-5.3"
    assert redaction.sanitize({"session_id": "s\u20601"})["session_id"] == "s1"


def test_non_ignorable_rare_letters_are_not_stripped():
    # Guard against over-stripping: an ordinary (non-default-ignorable)
    # letter — Soyombo Letter SHA, category Lo — must survive unchanged.
    assert redaction.sanitize({"model": "sk-𑩿fake"})["model"] == "sk-𑩿fake"
