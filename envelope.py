"""R-OBS-00 correlation-envelope construction and emission (HTB-B1).

Builds envelope dicts conforming — verbatim — to the pinned schema at
``schemas/correlation-envelope.schema.json`` (version 1.0.0, id
``urn:sovav-hermes:telemetry:correlation-envelope:1.0.0``, byte-identical to
the R-OBS-00 source; enforced by tests/test_envelope_schema_drift.py against
the pinned sha256 constants).

Emission is METADATA ONLY (telemetry-contract.md §3.1): the envelope carries
ids, profile/role, model/provider names and a call-path class; the sibling
``event`` body carries allowlisted metrics only, passed through
``redaction.sanitize()`` BEFORE anything is stored or handed off. Nothing is
persisted here — ``emit()`` returns the record and keeps a bounded IN-MEMORY
ring for this branch only; durable transport is B3's (transport.py), which
must consume the already-redacted record, never raw hook kwargs.

Envelope field derivation (base required fields — schema forbids nulls):

=================== =======================================================
Field               Source (first match wins)
=================== =======================================================
board               ``HERMES_KANBAN_BOARD`` env → ``"hermes"`` default
task_id             ``HERMES_KANBAN_TASK`` env → ``"unknown"`` sentinel
run_id              ``HERMES_KANBAN_RUN_ID`` env → ``"unknown"`` sentinel
parent_task_ids     ``HERMES_KANBAN_PARENT_TASK_IDS`` env (comma-sep) → ``[]``
profile/role        caller-supplied ``ctx.profile_name`` → ``"unknown"``
model               hook kwarg (effective model) → ``"unknown"``
provider            hook kwarg → ``"unknown"``
key_id              ``HERMES_TELEMETRY_KEY_ID`` env (NON-SECRET label) →
                    ``"unlabeled"``; never a key value
call_class          derived: ``platform == "cron"`` → ``cron``;
                    hook family ``subagent_*`` → ``delegate``; else
                    ``primary`` (``fallback``/``aux``/``judge`` reserved)
handoff_id          always ``None`` from hooks (no handoff semantics yet)
worktree/branch     ``HERMES_KANBAN_BRANCH`` env → lazy one-shot
                    ``git rev-parse --abbrev-ref HEAD`` in cwd → ``no-git``
trace_id            ``trc-`` + sha256(session_id)[:24] (deterministic,
                    stateless, per-session stable)
harness_task_id /   ``TURNSTONE_HARNESS_TASK_ID`` /
harness_run_id      ``TURNSTONE_HARNESS_RUN_ID`` env when BOTH set
                    (pair-consistency rule, contract §1.5); else null
=================== =======================================================

Turnstone extension fields (repo, agent_run_id, correlation_id, ...) are
always ``null`` here: the plugin is harness-originated (contract §1.2/§1.3).

Kill switch: set ``HERMES_TELEMETRY_ENVELOPE_ENABLED=0`` (or false/off) to
disable emission entirely. Emission is fail-open: it can never break a hook,
never touches the budget verdict path, and never writes to disk.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
from collections import deque
from pathlib import Path
from typing import Any

from . import redaction

# ---------------------------------------------------------------------------
# Pinned contract constants (TM-verified 2026-09-19). Do NOT edit to "fix" a
# drift failure — a mismatch means the pinned copy was tampered with.
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.0.0"
SCHEMA_ID = "urn:sovav-hermes:telemetry:correlation-envelope:1.0.0"
SCHEMA_SHA256 = "5271d5faa8bee8ad3d58885f4ab54f88d77af6e0620c78ba627665ca8433e70a"
CONTRACT_SHA256 = "599e38f3a403e1b39b67b0b8aa05daa7ce922890b602d53ff80fcdbbb55eb657"

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "correlation-envelope.schema.json"

# Envelope property names exactly as defined in the pinned schema (additional-
# properties forbidden; the slash characters are part of the canonical names).
_BASE_FIELDS = (
    "board",
    "task_id",
    "run_id",
    "parent_task_ids",
    "profile/role",
    "model",
    "provider",
    "key_id",
    "call_class",
    "handoff_id",
    "worktree/branch",
    "trace_id",
)
_TURNSTONE_EXTENSIONS = (
    "repo",
    "agent_run_id",
    "correlation_id",
    "idempotency_key",
    "deal_id",
    "worker_id",
    "model_ref",
    "trigger_type",
    "runtime_environment",
    "basis_fingerprint",
    "worker_definition_version",
    "output_schema_version",
)
_PROPAGATION_FIELDS = ("harness_task_id", "harness_run_id")

_CALL_CLASSES = frozenset({"primary", "fallback", "aux", "delegate", "judge", "cron"})

# Non-empty fallback sentinels (schema requires minLength 1 strings).
DEFAULT_BOARD = "hermes"
UNKNOWN = "unknown"
UNLABELED_KEY = "unlabeled"
NO_GIT_BRANCH = "no-git"

_BUFFER_SIZE = 1000
_buffer: deque = deque(maxlen=_BUFFER_SIZE)
_buffer_lock = threading.Lock()

_branch_cache_lock = threading.Lock()
_branch_cache: str | None = None

logger_name = "hermes_telemetry"


def schema_path() -> Path:
    """Absolute path of the pinned schema copy."""
    return _SCHEMA_PATH


def kill_switch_enabled() -> bool:
    """HERMES_TELEMETRY_ENVELOPE_ENABLED — on unless explicitly disabled
    (``0``/``false``/``off``, case-insensitive)."""
    raw = os.environ.get("HERMES_TELEMETRY_ENVELOPE_ENABLED", "")
    return raw.strip().lower() not in {"0", "false", "off"}


# ---------------------------------------------------------------------------
# Field derivation helpers
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def resolve_board() -> str:
    return _env("HERMES_KANBAN_BOARD") or DEFAULT_BOARD


def resolve_task_id() -> str:
    return _env("HERMES_KANBAN_TASK") or UNKNOWN


def resolve_run_id() -> str:
    return _env("HERMES_KANBAN_RUN_ID") or UNKNOWN


def resolve_parent_task_ids() -> list[str]:
    raw = _env("HERMES_KANBAN_PARENT_TASK_IDS")
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def resolve_key_id() -> str:
    # NON-SECRET credential label only. If an operator pastes an actual secret
    # into this env var, the value scrubber still runs over the final envelope.
    return _env("HERMES_TELEMETRY_KEY_ID") or UNLABELED_KEY


def resolve_branch() -> str:
    """One-shot per-process branch resolution (cached)."""
    global _branch_cache
    if _branch_cache is not None:
        return _branch_cache
    with _branch_cache_lock:
        if _branch_cache is not None:
            return _branch_cache
        env_branch = _env("HERMES_KANBAN_BRANCH")
        if env_branch:
            _branch_cache = env_branch
            return env_branch
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            branch = (proc.stdout or "").strip()
            _branch_cache = branch if proc.returncode == 0 and branch else NO_GIT_BRANCH
        except Exception:
            _branch_cache = NO_GIT_BRANCH
        return _branch_cache


def trace_id_for(session_id: str) -> str:
    """Deterministic per-session cross-repo trace id (contract §1.4)."""
    digest = hashlib.sha256((session_id or "").encode("utf-8", "replace")).hexdigest()[:24]
    return f"trc-{digest}"


def derive_call_class(platform: str, hook: str) -> str:
    if (platform or "").strip().lower() == "cron":
        return "cron"
    if (hook or "").startswith("subagent_"):
        return "delegate"
    return "primary"


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def build_envelope(
    session_id: str = "",
    model: str = "",
    provider: str = "",
    platform: str = "",
    profile: str = "",
    call_class: str | None = None,
    handoff_id: str | None = None,
) -> dict[str, Any]:
    """Build one envelope dict conforming to the pinned schema.

    Every base field is present and non-null (schema requires them); every
    Turnstone extension and harness-propagation field is null unless the
    TURNSTONE_HARNESS_* env pair is set. ``handoff_id`` is required-but-
    nullable and stays None unless a caller supplies one.
    """
    env = {
        "board": resolve_board(),
        "task_id": resolve_task_id(),
        "run_id": resolve_run_id(),
        "parent_task_ids": resolve_parent_task_ids(),
        "profile/role": (profile or "").strip() or UNKNOWN,
        "model": (model or "").strip() or UNKNOWN,
        "provider": (provider or "").strip() or UNKNOWN,
        "key_id": resolve_key_id(),
        "call_class": call_class or derive_call_class(platform, ""),
        "handoff_id": handoff_id,
        "worktree/branch": resolve_branch(),
        "trace_id": trace_id_for(session_id),
    }
    harness_task = _env("TURNSTONE_HARNESS_TASK_ID")
    harness_run = _env("TURNSTONE_HARNESS_RUN_ID")
    harness_pair = (harness_task, harness_run) if harness_task and harness_run else (None, None)
    for field in _TURNSTONE_EXTENSIONS:
        env[field] = None
    env["harness_task_id"], env["harness_run_id"] = harness_pair
    # No coercion here: an invalid call_class must surface via
    # structural_errors() rather than being silently rewritten — emit() derives
    # a valid class itself, so only direct callers can trip this.
    return env


def structural_errors(envelope: dict[str, Any]) -> list[str]:
    """Dependency-free always-on conformance check (schema-shaped).

    Returns a list of violations; empty list means the envelope satisfies the
    pinned schema's hard constraints: exact key set (additionalProperties
    false), required base fields non-null/non-empty, call_class enum, and the
    harness pair-consistency rule. The authoritative check (jsonschema against
    the pinned file) runs in tests.
    """
    errors: list[str] = []
    expected = set(_BASE_FIELDS) | set(_TURNSTONE_EXTENSIONS) | set(_PROPAGATION_FIELDS)
    actual = set(envelope)
    for missing in sorted(expected - actual):
        errors.append(f"missing field: {missing}")
    for extra in sorted(actual - expected):
        errors.append(f"unexpected field: {extra}")
    for field in _BASE_FIELDS:
        val = envelope.get(field)
        if field == "handoff_id":
            if val is not None and not isinstance(val, str):
                errors.append("handoff_id must be string or null")
            continue
        if field == "parent_task_ids":
            if not isinstance(val, list) or any(not isinstance(p, str) for p in val):
                errors.append("parent_task_ids must be an array of strings")
            continue
        if field == "run_id":
            if not (isinstance(val, (int, str)) and not isinstance(val, bool)):
                errors.append("run_id must be integer or string")
            continue
        if not isinstance(val, str) or len(val) < 1:
            errors.append(f"base field must be non-empty string: {field}")
    if envelope.get("call_class") not in _CALL_CLASSES:
        errors.append(f"call_class not in enum: {envelope.get('call_class')!r}")
    # Harness pair-consistency (contract §1.5).
    ht, hr = envelope.get("harness_task_id"), envelope.get("harness_run_id")
    if (ht is None) != (hr is None):
        errors.append("harness_task_id/harness_run_id must be carried together")
    # Non-Turnstone events must carry null extensions (contract §1.3).
    if envelope.get("repo") != "turnstone":
        for field in _TURNSTONE_EXTENSIONS:
            if envelope.get(field) is not None:
                errors.append(f"extension field must be null off Turnstone: {field}")
    return errors


def validate_envelope_with_jsonschema(envelope: dict[str, Any]) -> list[str]:
    """Authoritative jsonschema validation against the pinned copy.

    Requires the ``jsonschema`` package; returns error strings (empty = valid).
    """
    import json

    import jsonschema

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    validator = validator_cls(schema)
    return [
        f"{list(err.absolute_path)}: {err.message}"
        for err in sorted(validator.iter_errors(envelope), key=str)
    ]


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def emit(
    hook: str,
    session_id: str = "",
    model: str = "",
    provider: str = "",
    platform: str = "",
    profile: str = "",
    metadata: dict[str, Any] | None = None,
    handoff_id: str | None = None,
) -> dict[str, Any] | None:
    """Emit one metadata-only record for a hook capture point.

    Returns ``{"envelope": ..., "event": ...}`` (also kept in the bounded
    in-memory ring) or ``None`` when disabled. Fully fail-open: any internal
    error is swallowed and logged at debug — emission must never break a hook
    and never sits on the budget verdict path.
    """
    try:
        if not kill_switch_enabled():
            return None
        env = build_envelope(
            session_id=session_id,
            model=model,
            provider=provider,
            platform=platform,
            profile=profile,
            handoff_id=handoff_id,
        )
        # call_class derives from the actual hook name, not just platform.
        env["call_class"] = derive_call_class(platform, hook)
        # Defense in depth: envelope fields are built from hook kwargs (model
        # id, provider, profile), so run the value-level secret scrubber over
        # them too — a secret-shaped string can never ride on any field.
        env = redaction.sanitize(env, allowlist=frozenset(env))
        errors = structural_errors(env)
        if errors:  # never emit a non-conforming envelope
            import logging

            logging.getLogger(logger_name).debug(
                "envelope conformance check failed for hook=%s: %s", hook, errors
            )
            return None
        event = dict(metadata or {})
        event.setdefault("hook", hook)
        event.setdefault("session_id", session_id)
        redacted = redaction.sanitize(event)
        record = {"envelope": env, "event": redacted}
        with _buffer_lock:
            _buffer.append(record)
        return record
    except Exception:
        import logging

        logging.getLogger(logger_name).debug(
            "envelope emit failed for hook=%s", hook, exc_info=True
        )
        return None


def recent_records(limit: int | None = None) -> list[dict[str, Any]]:
    """Snapshot of the in-memory ring (oldest first). Test/diagnostic only."""
    with _buffer_lock:
        items = list(_buffer)
    return items[-limit:] if limit else items


def clear_records() -> None:
    """Reset the in-memory ring (test hook)."""
    with _buffer_lock:
        _buffer.clear()


def reset_derivation_cache() -> None:
    """Reset per-process caches (branch resolution; test hook)."""
    global _branch_cache
    with _branch_cache_lock:
        _branch_cache = None
