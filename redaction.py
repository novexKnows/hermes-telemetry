"""Allowlist-only redaction for correlation-envelope emission (HTB-B1).

Design authority: docs/telemetry/telemetry-contract.md §3 (shared redaction
rules) — the export default is METADATA ONLY, and redaction applies BEFORE
export: an emission path may never see un-redacted material and then rely on
the sink's scrubbing.

Two layers, both fail-closed:

1. **Key allowlist** — ``sanitize()`` recursively keeps ONLY keys in
   ``ALLOWED_EVENT_KEYS``. Anything else (prompt text, tool results, tool
   args, error message prose, file contents, secrets) is DROPPED, never
   logged, never echoed. Dropped keys are counted, not named.
2. **Value-level secret scrubbing** — defense in depth. Any string that
   matches a known secret SHAPE (API key prefixes, bearer tokens, DSNs,
   private-key blocks, credentialed URLs) is replaced with a fixed sentinel
   before it can ride on an allowlisted metadata field (e.g. a model id or
   provider string copied from an adversarial hook payload).

The sanitizer never raises and never logs payload content. It is applied at
emission time inside envelope.emit() — BEFORE the in-memory buffer or any
future transport (B3) ever sees the record.
"""

from __future__ import annotations

import re
import threading
from typing import Any

# ---------------------------------------------------------------------------
# Key allowlist — the ONLY event-body keys an emitted record may carry.
# Metadata only: ids, roles/profiles, model/provider names, token/latency
# counters, statuses, booleans. Conspicuously ABSENT: user_message,
# conversation_history, args, result, error_message, child_goal,
# child_summary, api_kwargs, request_messages, response text.
# ---------------------------------------------------------------------------

ALLOWED_EVENT_KEYS: frozenset[str] = frozenset(
    {
        # event identity / correlation
        "hook",
        "session_id",
        "task_id",
        "tool_call_id",
        "sender_id",
        "cron_job_id",
        "profile",
        "platform",
        # model attribution
        "model",
        "provider",
        "moa_preset",
        # api-call metrics (post_api_request / pre_api_request)
        "api_call_count",
        "approx_input_tokens",
        "latency_ms",
        "duration_ms",
        "finish_reason",
        "tokens_in",
        "tokens_out",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "cost_usd",
        "estimated",
        "provider_assumed",
        "assistant_content_chars",
        "moa_reference_count",
        # tool metrics
        "tool_name",
        "ok",
        "blocked",
        # status / error metadata (codes and enums, never prose)
        "status",
        "status_code",
        "error_type",
        "retryable",
        "retry_count",
        "occurrences",
        # subagent correlation (ids and enums, never goal/summary text)
        "parent_session_id",
        "child_session_id",
        "parent_turn_id",
        "parent_subagent_id",
        "child_subagent_id",
        "child_role",
        "child_status",
        "is_first_turn",
    }
)

REDACTED = "[REDACTED]"

# ---------------------------------------------------------------------------
# Secret-shape patterns (value scrubbing). Ordered; each match is replaced
# with REDACTED. Deliberately conservative shapes — must not false-positive
# on ordinary model ids (e.g. "gpt-4o", "z-ai/glm-5.3-flash") or provider
# names. Test markers like sk-FAKE-TEST-KEY-... match by design.
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    # OpenAI-style keys: sk-... (incl. sk-proj-, sk-FAKE-... test markers)
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    # Anthropic-style keys
    re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"),
    # GitHub tokens
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    # AWS
    re.compile(r"AKIA[0-9A-Z]{16}"),
    # Slack
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    # Bearer / token headers
    re.compile(r"(?:Bearer|bearer|Token|token)\s+[A-Za-z0-9._~+/-]{16,}={0,2}"),
    # Sentry DSNs
    re.compile(r"https://[0-9a-f]{16,}@[a-z0-9.-]+/[0-9a-f]*", re.IGNORECASE),
    # Private key blocks
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # URLs embedding credentials: scheme://user:secret@host
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
)

_scrub_hits_lock = threading.Lock()
_scrub_hits = 0


def _bump_scrub_hits(n: int) -> None:
    global _scrub_hits
    with _scrub_hits_lock:
        _scrub_hits += n


def scrub_hits() -> int:
    """Number of value-level secret scrubs performed since process start
    (or the last reset_stats()). Diagnostic counter only — no content."""
    with _scrub_hits_lock:
        return _scrub_hits


def reset_stats() -> None:
    """Zero the diagnostic counters (test hook)."""
    global _scrub_hits
    with _scrub_hits_lock:
        _scrub_hits = 0


def _scrub_string(value: str) -> str:
    out = value
    hits = 0
    for pattern in _SECRET_PATTERNS:
        out, n = pattern.subn(REDACTED, out)
        hits += n
    if hits:
        _bump_scrub_hits(hits)
    return out


def sanitize(value: Any, allowlist: frozenset[str] | None = None) -> Any:
    """Recursively filter ``value`` down to allowlisted keys, scrubbing
    secret-shaped strings. Never raises. Never logs.

    - dict  → keep only allowlisted keys (others dropped silently), recurse
    - list/tuple → recurse over elements
    - str   → secret-pattern scrub
    - scalars/None → returned as-is
    """
    allowed = ALLOWED_EVENT_KEYS if allowlist is None else allowlist
    try:
        return _sanitize_inner(value, allowed)
    except Exception:
        # Fail closed: on any internal error emit nothing of the payload.
        return {}


def _sanitize_inner(value: Any, allowed: frozenset[str]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            if key not in allowed:
                continue  # dropped, never logged
            out[str(key)] = _sanitize_inner(val, allowed)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_inner(item, allowed) for item in value]
    if isinstance(value, str):
        return _scrub_string(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    # Any other type (bytes, objects) is not representable metadata — drop.
    return None
